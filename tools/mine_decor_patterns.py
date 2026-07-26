#!/usr/bin/env python3
"""Mine per-item decoration placement patterns from the hand-authored corpus.

Emits, for each static item: where it sits relative to walls, what wall
material backs it, how open its surroundings are, how close it is to doors,
whether it forms runs/colonnades or flanking pairs, and what it co-occurs
with.  Also emits the INVERSE table (P(item | context bucket)) which is what
a generator actually needs to pick an item for a candidate cell.
"""
import argparse, sys, zipfile, glob, statistics as st
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from infiniwolf import tiles as G
from inspect_map import parse_wad

# The hand-authored reference corpus: sibling ECWolf mods installed alongside
# this repo. Override with --corpus when they live elsewhere.
DEFAULT_CORPUS = str(ROOT.parent / 'installed' / '*' / '*.pk3')
DEFAULT_OUT = ROOT / 'docs' / 'decor-corpus-patterns.md'

GRID = 64
BLOCK = set(G.STATIC_BLOCKING)
OPEN_ = set(G.STATIC_OPEN)
DECOR = BLOCK | OPEN_
FLOORMIN = 106
N4 = ((1, 0), (-1, 0), (0, 1), (0, -1))

NAMES = {24:'GreenBarrel',25:'TableChairs',26:'FloorLamp',28:'HangedMan',30:'WhitePillar',
31:'GreenPlant',33:'Sink',34:'BrownPlant',35:'Vase',36:'BareTable',39:'SuitOfArmor',
40:'HangingCage',41:'SkeletonCage',45:'BunkBed',58:'Barrel',59:'Well',60:'EmptyWell',
62:'Flag',68:'Stove',23:'Puddle',27:'Chandelier',32:'SkeletonFlat',37:'CeilingLight',
38:'KitchenStuff',42:'Bones1',46:'Basket',57:'Gibs',61:'Blood',64:'Bones2',65:'Bones3',
66:'Bones4',67:'Pots'}

MATERIAL = {}
for fam in G.WALL_MATERIALS:
    for t in (fam.base,) + tuple(fam.accents):
        MATERIAL[t] = fam.name


def load(pattern=DEFAULT_CORPUS):
    maps = []
    for f in sorted(glob.glob(pattern)):
        if 'infiniwolf' in f:
            continue
        mod = f.split('/')[-2]
        z = zipfile.ZipFile(f)
        for n in z.namelist():
            if not n.lower().endswith('.wad'):
                continue
            p = parse_wad(z.read(n))
            if p and sum(1 for v in p[1] if v in DECOR) >= 5:
                maps.append((mod, n, p[0], p[1]))
    return maps


def analyse(maps):
    at = lambda p, x, y: p[y * GRID + x] if 0 <= x < GRID and 0 <= y < GRID else 0
    ctx = defaultdict(Counter)        # item -> Counter of feature values
    bucket_items = defaultdict(Counter)   # context bucket -> Counter of items
    cooc = defaultdict(Counter)
    runs = defaultdict(Counter)       # item -> Counter of (stride, length)
    flank = defaultdict(Counter)      # item -> Counter of gap distance
    backing = defaultdict(Counter)    # item -> Counter of wall material
    openness_vals = defaultdict(list)
    doordist = defaultdict(list)
    total = Counter()

    for mod, name, tiles, things in maps:
        isfloor = lambda x, y: at(tiles, x, y) >= FLOORMIN
        iswall = lambda x, y: at(tiles, x, y) < FLOORMIN and at(tiles, x, y) not in G.DOORS
        doors = [(x, y) for y in range(GRID) for x in range(GRID)
                 if at(tiles, x, y) in G.DOORS]
        cells = [(x, y) for y in range(GRID) for x in range(GRID)
                 if at(things, x, y) in DECOR]
        for (x, y) in cells:
            item = at(things, x, y)
            total[item] += 1
            walls = [(dx, dy) for dx, dy in N4 if iswall(x + dx, y + dy)]
            nw = len(walls)
            # openness: floor cells in the 5x5 box
            op = sum(1 for yy in range(y - 2, y + 3) for xx in range(x - 2, x + 3)
                     if isfloor(xx, yy))
            openness_vals[item].append(op)
            # geometry class
            if nw == 0:
                geo = 'free'
            elif nw == 1:
                geo = 'wall'
            elif nw == 2:
                geo = 'corner' if walls[0][0] != walls[1][0] and walls[0][1] != walls[1][1] else 'slot'
            else:
                geo = 'nook'
            ctx[item]['geo_' + geo] += 1
            space = 'tight' if op <= 9 else ('medium' if op <= 17 else 'open')
            ctx[item]['space_' + space] += 1
            bucket_items[(geo, space)][item] += 1
            # backing material
            for dx, dy in walls:
                backing[item][at(tiles, x + dx, y + dy)] += 1
            # door distance
            if doors:
                dd = min(abs(x - dx2) + abs(y - dy2) for dx2, dy2 in doors)
                doordist[item].append(dd)
                if dd <= 2:
                    ctx[item]['near_door'] += 1
            # co-occurrence within radius 2
            for yy in range(y - 2, y + 3):
                for xx in range(x - 2, x + 3):
                    if (xx, yy) != (x, y) and at(things, xx, yy) in DECOR:
                        cooc[item][at(things, xx, yy)] += 1
            # flanking pair: identical item mirrored about this cell's axis
            for d in (1, 2, 3):
                for dx, dy in ((1, 0), (0, 1)):
                    a = at(things, x - d * dx, y - d * dy)
                    b = at(things, x + d * dx, y + d * dy)
                    if a == item and b == item:
                        flank[item][d] += 1

        # straight runs of identical items at constant stride
        for item in set(at(things, x, y) for x, y in cells):
            pts = {(x, y) for x, y in cells if at(things, x, y) == item}
            for stride in (1, 2, 3, 4):
                for horiz in (True, False):
                    seen = set()
                    for (x, y) in sorted(pts):
                        if (x, y, stride, horiz) in seen:
                            continue
                        L = 1
                        cx, cy = x, y
                        while True:
                            nx, ny = (cx + stride, cy) if horiz else (cx, cy + stride)
                            if (nx, ny) in pts:
                                seen.add((nx, ny, stride, horiz))
                                cx, cy = nx, ny
                                L += 1
                            else:
                                break
                        if L >= 3:
                            runs[item][(stride, L)] += 1
    return dict(ctx=ctx, bucket_items=bucket_items, cooc=cooc, runs=runs,
                flank=flank, backing=backing, openness=openness_vals,
                doordist=doordist, total=total)


def report(r, nmaps, out):
    P = lambda *a: print(*a, file=out)
    total = r['total']
    grand = sum(total.values())
    P(f"# Decoration placement patterns — {nmaps} hand-authored maps, "
      f"{grand} decoration instances\n")

    P("## Per-item placement profile\n")
    P("geo: free=no wall nbr, wall=1, corner=2 perpendicular, slot=2 opposite "
      "(1-wide), nook=3+")
    P("space: tight=<=9 floor cells in 5x5, medium=10-17, open=>=18\n")
    hdr = (f"{'item':14s}{'kind':6s}{'n':>6s}{'/map':>6s}"
           f"{'free':>6s}{'wall':>6s}{'cornr':>6s}{'slot':>6s}{'nook':>6s}"
           f"{'tight':>6s}{'med':>6s}{'open':>6s}{'door<=2':>8s}{'medOpen':>8s}")
    P(hdr)
    P("-" * len(hdr))
    for item, n in total.most_common():
        c = r['ctx'][item]
        g = lambda k: 100.0 * c[k] / n
        P(f"{NAMES.get(item,str(item)):14s}"
          f"{'BLOCK' if item in BLOCK else 'open':6s}{n:6d}{n/nmaps:6.1f}"
          f"{g('geo_free'):6.0f}{g('geo_wall'):6.0f}{g('geo_corner'):6.0f}"
          f"{g('geo_slot'):6.0f}{g('geo_nook'):6.0f}"
          f"{g('space_tight'):6.0f}{g('space_medium'):6.0f}{g('space_open'):6.0f}"
          f"{g('near_door'):8.0f}"
          f"{st.median(r['openness'][item]):8.0f}")

    P("\n\n## Wall material backing each item (top 4, % of its wall contacts)\n")
    for item, n in total.most_common():
        b = r['backing'][item]
        tot = sum(b.values())
        if tot < 30:
            continue
        top = ", ".join(f"{MATERIAL.get(t, 'tile'+str(t))}({t})={100*v/tot:.0f}%"
                        for t, v in b.most_common(4))
        P(f"{NAMES.get(item,str(item)):14s} n={tot:6d}  {top}")

    P("\n\n## Runs / colonnades — straight lines of >=3 identical items\n")
    P(f"{'item':14s}{'runs':>7s}{'top (stride,len) patterns':>10s}")
    for item, n in total.most_common():
        rr = r['runs'][item]
        if not rr:
            continue
        tot = sum(rr.values())
        top = "  ".join(f"s{s}xL{L}={v}" for (s, L), v in rr.most_common(6))
        P(f"{NAMES.get(item,str(item)):14s}{tot:7d}   {top}")

    P("\n\n## Flanking pairs — identical item mirrored at +/-d about a cell\n")
    P(f"{'item':14s}{'n':>7s}{'rate':>7s}  gap distribution")
    for item, n in total.most_common():
        f = r['flank'][item]
        if not f:
            continue
        tot = sum(f.values())
        dist = "  ".join(f"d={d}:{100*v/tot:.0f}%" for d, v in sorted(f.items()))
        P(f"{NAMES.get(item,str(item)):14s}{tot:7d}{100*tot/n:6.0f}%  {dist}")

    P("\n\n## Co-occurrence — what sits within 2 cells (top 5 partners)\n")
    for item, n in total.most_common():
        c = r['cooc'][item]
        if sum(c.values()) < 50:
            continue
        tot = sum(c.values())
        top = ", ".join(f"{NAMES.get(k,str(k))}={100*v/tot:.0f}%"
                        for k, v in c.most_common(5))
        P(f"{NAMES.get(item,str(item)):14s} {top}")

    P("\n\n## INVERSE MODEL — P(item | geometry, space) : what to place on a "
      "candidate cell\n")
    for key in sorted(r['bucket_items'], key=lambda k: -sum(r['bucket_items'][k].values())):
        c = r['bucket_items'][key]
        tot = sum(c.values())
        if tot < 100:
            continue
        geo, space = key
        P(f"\n--- {geo:6s} / {space:6s}   n={tot}  ({100*tot/grand:.1f}% of all decor)")
        acc = 0
        for k, v in c.most_common(10):
            acc += v
            P(f"      {NAMES.get(k,str(k)):14s}{'BLOCK' if k in BLOCK else 'open':6s}"
              f"{100*v/tot:6.1f}%   (cum {100*acc/tot:.0f}%)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--corpus', default=DEFAULT_CORPUS,
                    help='glob for hand-authored .pk3 files (default: %(default)s)')
    ap.add_argument('--out', default=str(DEFAULT_OUT),
                    help='report destination, or "-" for stdout (default: %(default)s)')
    args = ap.parse_args(argv)

    maps = load(args.corpus)
    if not maps:
        raise SystemExit(f"no maps matched {args.corpus!r}")
    res = analyse(maps)
    if args.out == '-':
        report(res, len(maps), sys.stdout)
        return
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('w') as fh:
        report(res, len(maps), fh)
    print("maps:", len(maps), "-> wrote", dest)


if __name__ == '__main__':
    main()
