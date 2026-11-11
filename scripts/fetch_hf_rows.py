"""Fetch a random sample of rows from the HF dataset-viewer API into JSONL.
argv: dataset total_rows n_requests per_request out.jsonl [seed]"""
import json, random, sys, time, urllib.request
ds, total, nreq, per, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
rng = random.Random(int(sys.argv[6]) if len(sys.argv) > 6 else 1)
n = 0
with open(out, "w") as fh:
    for i in range(nreq):
        off = rng.randrange(0, total - per)
        url = f"https://datasets-server.huggingface.co/rows?dataset={ds}&config=default&split=train&offset={off}&length={per}"
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "llm-wrapper-research"})
                data = json.load(urllib.request.urlopen(req, timeout=120))
                break
            except Exception as exc:
                print("retry", off, exc, file=sys.stderr); time.sleep(3 * (attempt + 1))
        else:
            continue
        for r in data["rows"]:
            fh.write(json.dumps(r["row"]) + "\n"); n += 1
        time.sleep(0.5)
print(out, n, "rows")
