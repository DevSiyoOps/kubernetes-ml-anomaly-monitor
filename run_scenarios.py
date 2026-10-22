#!/usr/bin/env python3
"""Run labelled workload scenarios against hpa-app and record exactly when each load ran.

  python3 run_scenarios.py list                         # show the scenarios
  python3 run_scenarios.py run normal_steady --dry-run  # print the plan and pod commands only
  python3 run_scenarios.py run normal_steady attack_flood --scale 0.4    # short pilot
  python3 run_scenarios.py run all                      # the full set, about 2.5 h
  python3 run_scenarios.py run attack_yoyo --suffix _r2 # a repeat, saved as attack_yoyo_r2

For every run this writes
  data/runs/<run>/labels.csv   one row per load segment: phase, kind, label, event_id, start, end,
                               workers, est_rps (start/end are Unix seconds in the cluster's clock)
  data/runs/<run>/meta.json    run window, scenario, scale, clock offset, HPA settings, app image

How the load is made: before the run starts, one busybox pod per load segment is created. Each pod
waits for its exact start time, runs N parallel wget loops against http://hpa-app/work?ms=M until
its exact end time, then exits. Creating the pods up front means pod start-up time does not shift
the labels.

Clock: the pods and Prometheus use the Docker VM clock, which can differ from the Mac clock. The
offset is measured once per run (docker exec on the kind node) and every time written is in the
VM clock, so the labels line up with the Prometheus timestamps.

Before each run the script waits until the HPA is back at its minimum replica count, so every run
starts from the same state.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

NODE = os.getenv("KIND_NODE", "hpa-lab-control-plane")
NS = os.getenv("NS", "default")
HPA = os.getenv("HPA", "hpa-app")
TARGET_URL = os.getenv("TARGET_URL", "http://hpa-app/work")
IMAGE = "busybox:1.28"
RUNS_DIR = os.path.join("data", "runs")
LEAD_S = 45          # time between creating the pods and the first start, so every pod is Running
SETTLE_HOLD_S = 30   # the HPA must stay at its minimum this long before a run starts
REQ_OVERHEAD_S = 0.01  # rough per-request cost of wget + network, used only for est_rps

# Load shapes (workers = parallel wget loops, ms = CPU time asked per request, sleep between requests).
BASE = dict(workers=2, ms=40, sleep=0.5)      # ordinary background traffic, about 3.6 req/s
FLOOD = dict(workers=16, ms=10, sleep=0)      # many cheap requests
HEAVY = dict(workers=3, ms=800, sleep=0.2)    # few expensive requests (high CPU per request)
YOYO_ON = dict(workers=10, ms=40, sleep=0)    # short bursts timed against the HPA scale-down
BENIGN = dict(workers=4, ms=40, sleep=0.2)    # flash crowd that is still normal traffic


def seg(phase, kind, start, end, load, label="normal", event_id=""):
    return dict(phase=phase, kind=kind, label=label, event_id=event_id, start=start, end=end, **load)


def _normal_steady():
    return 900, [seg("baseline", "steady", 0, 900, BASE)]


def _normal_ramp():
    steps = [1, 2, 3, 4, 5, 4, 3, 2, 1]
    segs = [seg(f"ramp{i + 1}", "ramp", i * 120, (i + 1) * 120, dict(BASE, workers=w))
            for i, w in enumerate(steps)]
    return 1080, segs


def _normal_wave():
    pattern = [1, 2, 3, 4, 3, 2]
    segs = [seg(f"wave{i + 1}", "wave", i * 60, (i + 1) * 60, dict(BASE, workers=pattern[i % 6]))
            for i in range(20)]
    return 1200, segs


def _normal_bursts():
    segs = [seg("baseline", "steady", 0, 840, BASE)]
    for i, t in enumerate((180, 420, 660)):
        segs.append(seg(f"benign_burst{i + 1}", "benign_burst", t, t + 45, BENIGN))
    return 840, segs


def _attack(kind, load):
    def build():
        segs = [seg("baseline", "steady", 0, 1200, BASE)]
        for i, t in enumerate((300, 780)):
            segs.append(seg(f"{kind}{i + 1}", kind, t, t + 180, load, "attack", f"{kind}{i + 1}"))
        return 1200, segs
    return build


def _attack_yoyo():
    # 4 cycles of 75 s on / 105 s off between 180 s and 900 s. The whole oscillating window is one
    # attack episode (event_id yoyo1), including the quiet halves.
    segs = [seg("baseline", "steady", 0, 1020, BASE)]
    for i in range(4):
        t = 180 + i * 180
        segs.append(seg(f"yoyo_on{i + 1}", "yoyo", t, t + 75, YOYO_ON, "attack", "yoyo1"))
    return 1020, segs


SCENARIOS = {
    "normal_steady": ("constant background traffic", _normal_steady),
    "normal_ramp": ("traffic rising to 5x and back in 2 min steps", _normal_ramp),
    "normal_wave": ("traffic cycling 1x-4x every 6 min", _normal_wave),
    "normal_bursts": ("background plus three 45 s benign bursts (labelled normal)", _normal_bursts),
    "attack_flood": ("background plus two 3 min request floods", _attack("flood", FLOOD)),
    "attack_heavy": ("background plus two 3 min expensive-request attacks", _attack("heavy", HEAVY)),
    "attack_yoyo": ("background plus a 12 min on/off yo-yo attack", _attack_yoyo),
}


def est_rps(s):
    return s["workers"] / (s["ms"] / 1000 + s["sleep"] + REQ_OVERHEAD_S)


def build_scenario(name, scale=1.0):
    """(duration_s, segments) with all times scaled and rounded to whole seconds."""
    duration, segs = SCENARIOS[name][1]()
    out = []
    for s in segs:
        s = dict(s, start=int(round(s["start"] * scale)), end=int(round(s["end"] * scale)))
        if s["end"] > s["start"]:
            s["est_rps"] = round(est_rps(s), 2)
            out.append(s)
    return int(round(duration * scale)), out


# ---------------------------------------------------------------- cluster helpers

def kubectl(*args, check=True, capture=True):
    cmd = ["kubectl", "-n", NS, *args]
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {(r.stderr or '').strip()}")
    return r.stdout if capture else ""


def clock_offset():
    """VM clock minus Mac clock, in seconds (0 with a warning when the node cannot be reached)."""
    try:
        t0 = time.time()
        r = subprocess.run(["docker", "exec", NODE, "date", "+%s.%N"], capture_output=True, text=True, timeout=15)
        t1 = time.time()
        vm = float(r.stdout.strip())
        return round(vm - (t0 + t1) / 2, 3)
    except Exception as e:  # noqa: BLE001 - any failure means "unknown"
        print(f"  warning: could not read the clock of {NODE} ({e}); assuming offset 0")
        return 0.0


def hpa_info():
    try:
        return json.loads(kubectl("get", "hpa", HPA, "-o", "json"))
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"  warning: could not read HPA {HPA}: {e}")
        return {}


def hpa_settings(h):
    spec = h.get("spec", {})
    return {"minReplicas": spec.get("minReplicas", 1), "maxReplicas": spec.get("maxReplicas"),
            "metrics": spec.get("metrics"), "behavior": spec.get("behavior")}


def app_image():
    try:
        return kubectl("get", "deployment", HPA, "-o", "jsonpath={.spec.template.spec.containers[*].image}").strip()
    except RuntimeError:
        return None


def wait_for_settle(max_wait):
    """Wait until current replicas == minReplicas and desired == current for SETTLE_HOLD_S."""
    t_end = time.time() + max_wait
    held_since = None
    last_print = 0
    while time.time() < t_end:
        h = hpa_info()
        mn = h.get("spec", {}).get("minReplicas", 1)
        cur = h.get("status", {}).get("currentReplicas")
        des = h.get("status", {}).get("desiredReplicas")
        if cur == mn and des == mn:
            held_since = held_since or time.time()
            if time.time() - held_since >= SETTLE_HOLD_S:
                print(f"  HPA settled at {mn} replica(s)")
                return True
        else:
            held_since = None
        if time.time() - last_print > 30:
            print(f"  waiting for the HPA to settle: current={cur} desired={des} min={mn}")
            last_print = time.time()
        time.sleep(5)
    print(f"  warning: HPA did not settle within {max_wait}s, starting anyway (noted in meta.json)")
    return False


def pod_script(s, t_start, t_end):
    """Shell script for one load pod. t_start/t_end are VM-clock Unix seconds."""
    nap = f'; sleep {s["sleep"]}' if s["sleep"] else ""
    loop = (f'while [ "$(date +%s)" -lt {t_end} ]; do '
            f'wget -q -O /dev/null -T 10 "{TARGET_URL}?ms={s["ms"]}"{nap}; done')
    return (f'while [ "$(date +%s)" -lt {t_start} ]; do sleep 1; done; '
            f'i=0; while [ $i -lt {s["workers"]} ]; do ({loop}) & i=$((i+1)); done; wait')


def pod_name(run, i):
    return f"lg-{run}-{i}".replace("_", "-").lower()[:63]


def create_pods(run, segs, t0, dry_run):
    names = []
    for i, s in enumerate(segs):
        if s["workers"] <= 0:
            continue
        name = pod_name(run, i)
        cmd = ["run", name, f"--image={IMAGE}", "--restart=Never", "--image-pull-policy=IfNotPresent",
               f"--labels=role=loadgen,run={run.replace('_', '-')}", "--command", "--",
               "sh", "-c", pod_script(s, t0 + s["start"], t0 + s["end"])]
        if dry_run:
            print("    kubectl -n", NS, " ".join(shlex.quote(c) for c in cmd))
        else:
            kubectl(*cmd)
        names.append(name)
    return names


def delete_pods(run):
    kubectl("delete", "pod", "-l", f"role=loadgen,run={run.replace('_', '-')}", "--wait=false",
            "--ignore-not-found", check=False)


def pods_running(names):
    out = kubectl("get", "pods", "-l", "role=loadgen", "-o",
                  "jsonpath={range .items[*]}{.metadata.name} {.status.phase}{\"\\n\"}{end}", check=False)
    phase = dict(line.split() for line in out.splitlines() if len(line.split()) == 2)
    return [n for n in names if phase.get(n) != "Running"]


def write_labels(path, segs, t0):
    cols = ["phase", "kind", "label", "event_id", "start", "end", "workers", "ms", "sleep", "est_rps"]
    with open(path, "w") as f:
        f.write(",".join(cols) + "\n")
        for s in segs:
            row = dict(s, start=t0 + s["start"], end=t0 + s["end"])
            f.write(",".join(str(row[c]) for c in cols) + "\n")


def run_complete(out_dir):
    try:
        with open(os.path.join(out_dir, "meta.json")) as f:
            return json.load(f).get("complete", False)
    except (OSError, json.JSONDecodeError):
        return False


def run_one(scenario, args):
    run = scenario + args.suffix
    duration, segs = build_scenario(scenario, args.scale)
    out_dir = os.path.join(RUNS_DIR, run)
    print(f"\n=== {run}: {SCENARIOS[scenario][0]} ({duration / 60:.1f} min, {len(segs)} load segments)")
    for s in segs:
        print(f"    {s['start']:>5}-{s['end']:<5}s  {s['phase']:<14} {s['label']:<6} "
              f"workers={s['workers']:<2} ms={s['ms']:<4} sleep={s['sleep']:<4} ~{s['est_rps']} req/s")

    if args.dry_run:
        t0 = int(time.time()) + LEAD_S
        create_pods(run, segs, t0, dry_run=True)
        return
    if run_complete(out_dir) and not args.overwrite:
        print(f"  {out_dir} already exists; use --suffix for a repeat or --overwrite to replace it. Skipped.")
        return

    delete_pods(run)
    settled = wait_for_settle(args.cooldown)
    offset = clock_offset()
    h = hpa_info()
    t0 = int(time.time() + offset) + LEAD_S + 2 * len(segs)   # VM-clock start of the run
    names = create_pods(run, segs, t0, dry_run=False)
    os.makedirs(out_dir, exist_ok=True)
    meta = {
        "run": run, "scenario": scenario, "description": SCENARIOS[scenario][0],
        "start": t0, "end": t0 + duration, "duration_s": duration, "scale": args.scale,
        "clock": "docker VM (same as Prometheus)", "clock_offset_s": offset,
        "settled_before_start": settled, "hpa": hpa_settings(h), "app_image": app_image(),
        "load_image": IMAGE, "target_url": TARGET_URL, "synthetic": False, "complete": False,
        "created_local": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    write_labels(os.path.join(out_dir, "labels.csv"), segs, t0)

    vm_now = lambda: time.time() + offset  # noqa: E731
    while vm_now() < t0 - 3:
        time.sleep(1)
    missing = pods_running(names)
    if missing:
        print(f"  warning: not Running at the start time: {', '.join(missing)} (check: kubectl get pods -l role=loadgen)")

    last = -1
    while vm_now() < t0 + duration:
        minute = int((vm_now() - t0) // 60)
        if minute != last:
            st = hpa_info().get("status", {})
            print(f"  {run}: minute {minute:>2}/{duration // 60}  replicas={st.get('currentReplicas')} "
                  f"desired={st.get('desiredReplicas')}")
            last = minute
        time.sleep(2)
    delete_pods(run)
    meta["complete"] = True   # export_runs.py skips runs that were interrupted
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print(f"  done. Labels and meta in {out_dir}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("scenarios", nargs="+", help="scenario names, or 'all'")
    r.add_argument("--scale", type=float, default=1.0, help="multiply every duration (0.4 for a pilot)")
    r.add_argument("--cooldown", type=int, default=900, help="max seconds to wait for the HPA to settle")
    r.add_argument("--suffix", default="", help="appended to the run name, e.g. _r2 for a repeat")
    r.add_argument("--dry-run", action="store_true", help="print the plan and pod commands, change nothing")
    r.add_argument("--overwrite", action="store_true", help="replace an existing run folder")
    args = p.parse_args()

    if args.cmd == "list":
        total = 0
        for name, (desc, _) in SCENARIOS.items():
            d, segs = build_scenario(name)
            total += d
            print(f"  {name:<14} {d / 60:>5.1f} min  {desc}")
        print(f"  total {total / 60:.0f} min of load, plus settling time between runs")
        return

    names = list(SCENARIOS) if args.scenarios == ["all"] else args.scenarios
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        sys.exit(f"unknown scenario(s): {', '.join(unknown)}. Try: python3 run_scenarios.py list")
    current = None
    try:
        for current in names:
            run_one(current, args)
    except KeyboardInterrupt:
        print("\nInterrupted. Deleting the load pods of the current run.")
        if current and not args.dry_run:
            delete_pods(current + args.suffix)
        sys.exit(1)


if __name__ == "__main__":
    main()
