import time, random, torch
from sentence_transformers import SentenceTransformer
random.seed(0)
words = open("/usr/share/dict/words").read().split()
# realistic mixed-length chunks: 30..380 words
texts = [" ".join(random.choices(words, k=random.randint(30, 380))) for _ in range(1024)]
def run(dev, half=False, sort=False, bs=64):
    m = SentenceTransformer("all-mpnet-base-v2", device=dev)
    if half: m = m.half()
    t = sorted(texts, key=len) if sort else texts
    m.encode(t[:64], batch_size=bs)  # warmup
    if dev == "mps": torch.mps.synchronize()
    s = time.perf_counter(); m.encode(t, batch_size=bs)
    if dev == "mps": torch.mps.synchronize()
    return len(t) / (time.perf_counter() - s)
# note: sentence-transformers already length-sorts inside encode(); sort flag checks that
for label, kw in [("cpu fp32", dict(dev="cpu")), ("mps fp32", dict(dev="mps")),
                  ("mps fp16", dict(dev="mps", half=True)), ("mps fp16 bs128", dict(dev="mps", half=True, bs=128))]:
    print(f"{label:16s} {run(**kw):7.1f} chunks/s", flush=True)

# ---------------------------------------------------------------------------
# PROVENANCE (added 2026-09-20, after S0 reported this benchmark as unrecoverable)
#
# This is the verbatim script that produced the numbers quoted in the S0 prompt:
#   cpu fp32  10.7 | mps fp32  26.3 | mps fp16  33.7 | mps fp16 bs128  23-27
# It ran from a session scratchpad, which is why S0 could not find it in-repo.
#
# Note for anyone comparing against it:
#   - chunks are 30-380 WORDS (median ~2160 chars), NOT the product's 1000-char
#     chunk size. S0's reconstruction used 1000 chars fixed and was therefore
#     ~2.2x shorter, which is why it saw zero capped chunks.
#   - the headline 33.7 is fp16 at batch 64. The fp32 figure is 26.3.
#   - single timed pass per config, so it captures burst throughput before
#     thermal decay sets in.
# ---------------------------------------------------------------------------
