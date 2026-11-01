"""Сборка web/static/ru-map.json: python3 tools/build_ru_map.py ne_10m_admin_1_states_provinces.geojson web/static/ru-map.json
Исходник — Natural Earth (github.com/nvkelso/natural-earth-vector, geojson/), общественное достояние. Нужен Pillow.

Карта федеральных округов России для мониторинга BobrNet из Natural Earth 1:10m (общественное достояние).

Выход — web/static/ru-map.json: {w, h, districts: {код: {d: путь заливки, cx, cy}}, borders: путь границ округов,
coast: путь внешней границы, cities: {ключ: [x, y, округ]}, proj: параметры проекции}.
"""
import json
import math
import sys
from collections import defaultdict

SRC, OUT = sys.argv[1], sys.argv[2]

DIST = {
    'c': 'BEL BRY VLA VOR IVA KLU KOS KRS LIP MOS MOW ORL RYA SMO TAM TVE TUL YAR',
    'nw': 'KR KO ARK NEN VLG KGD LEN SPE MUR NGR PSK',
    's': 'AD KL KDA AST VGG ROS',
    'nc': 'DA IN KB KC SE CE STA',
    'v': 'BA ME MO TA UD CU PER KIR NIZ ORE PNZ SAM SAR ULY',
    'u': 'KGN SVE TYU KHM YAN CHE X01~',
    'sib': 'AL ALT TY KK KYA IRK KEM NVS OMS TOM',
    'fe': 'BU SA ZAB KAM PRI KHA AMU MAG SAK YEV CHU',
}
OF = {f'RU-{c}': k for k, v in DIST.items() for c in v.split()}

# Альберс: центральный меридиан 100°, параллели 52° и 64° — привычный вид России
LON0, LAT0, P1, P2 = 100.0, 56.0, 52.0, 64.0
r = math.radians
n = (math.sin(r(P1)) + math.sin(r(P2))) / 2
C = math.cos(r(P1)) ** 2 + 2 * n * math.sin(r(P1))
rho0 = math.sqrt(C - 2 * n * math.sin(r(LAT0))) / n


def proj(lon, lat):
    if lon < 0:
        lon += 360  # Чукотка за 180-м меридианом
    rho = math.sqrt(C - 2 * n * math.sin(r(lat))) / n
    th = n * r(lon - LON0)
    return rho * math.sin(th), rho0 - rho * math.cos(th)


def rings_of(g):
    if g['type'] == 'Polygon':
        return [g['coordinates']]
    return g['coordinates']


d = json.load(open(SRC))
feats = [f for f in d['features'] if f['properties'].get('iso_3166_2') in OF]
assert len(feats) == 84, len(feats)

polys = []  # (округ, [кольца в проекции])
for f in feats:
    k = OF[f['properties']['iso_3166_2']]
    for poly in rings_of(f['geometry']):
        polys.append((k, [[proj(lon, lat) for lon, lat in ring] for ring in poly]))

xs = [x for _, p in polys for ring in p for x, _ in ring]
ys = [y for _, p in polys for ring in p for _, y in ring]
minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
W = 1000.0
S = W / (maxx - minx)
H = (maxy - miny) * S


def px(pt):  # в пиксели: y вниз
    return ((pt[0] - minx) * S, (maxy - pt[1]) * S)


def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy)
    best, bi = -1.0, 0
    for i in range(1, len(pts) - 1):
        x0, y0 = pts[i]
        dist = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / L if L else math.hypot(x0 - x1, y0 - y1)
        if dist > best:
            best, bi = dist, i
    if best > eps:
        return rdp(pts[:bi + 1], eps)[:-1] + rdp(pts[bi:], eps)
    return [pts[0], pts[-1]]


def area(ring):
    return abs(sum(ring[i][0] * ring[i - 1][1] - ring[i - 1][0] * ring[i][1] for i in range(len(ring)))) / 2


EPS, MIN_AREA = 1.1, 6.0
sys.setrecursionlimit(100000)


def path(rings):
    out = []
    for ring in rings:
        pts = [px(p) for p in ring]
        if area(pts) < MIN_AREA:
            continue
        s = rdp(pts, EPS)
        if len(s) < 4:
            continue
        out.append('M' + 'L'.join(f'{x:.0f},{y:.0f}' for x, y in s[:-1]) + 'Z')
    return ''.join(out)


by = defaultdict(list)
for k, p in polys:
    by[k].extend(p)

# границы между округами: растр с номерами округов; ребро — граница, если по ту сторону другой округ
from PIL import Image, ImageDraw
K = list(DIST)
SC = 3
img = Image.new('L', (int(W * SC) + 4, int(H * SC) + 4), 0)
dr = ImageDraw.Draw(img)
simp = {}
for i, k in enumerate(K, 1):
    simp[k] = []
    for ring in by[k]:
        pts = [px(pt) for pt in ring]
        if area(pts) < MIN_AREA:
            continue
        sp = rdp(pts, EPS)
        if len(sp) >= 4:
            simp[k].append(sp)
            dr.polygon([(x * SC, y * SC) for x, y in sp], fill=i)
pix = img.load()


def at(x, y):
    X, Y = int(x * SC), int(y * SC)
    if 0 <= X < img.width and 0 <= Y < img.height:
        return pix[X, Y]
    return 0


borders = []
for i, k in enumerate(K, 1):
    for ring in simp[k]:
        run = []
        for a, b in zip(ring, ring[1:]):
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            dx, dy = b[0] - a[0], b[1] - a[1]
            L = math.hypot(dx, dy) or 1
            nx, ny = -dy / L * 2.2, dx / L * 2.2
            sides = {at(mx + nx, my + ny), at(mx - nx, my - ny)}
            other = [j for j in sides if j and j != i]
            if other and min(other) > i:  # каждую границу рисует один из двух округов
                if not run:
                    run = [a]
                run.append(b)
            elif run:
                borders.append(run)
                run = []
        if run:
            borders.append(run)
borders = ['M' + 'L'.join(f'{x:.0f},{y:.0f}' for x, y in r) for r in borders if len(r) >= 2]

# подписи округов: центр тяжести самого большого кольца
labels = {'c': (36.0, 52.2), 'nw': (40.5, 63.2), 's': (42.5, 48.3), 'nc': (45.5, 44.0), 'v': (51.5, 55.0),
          'u': (66.5, 61.0), 'sib': (95.0, 61.0), 'fe': (133.0, 63.5)}
CITIES = {  # ключевые слова в названии чекера → координаты
    'москв мск moscow msk': (37.62, 55.75), 'петербург спб питер spb': (30.32, 59.94), 'новосибирск нск nsk': (82.92, 55.03),
    'екатеринбург екб ekb': (60.6, 56.84), 'казан kazan': (49.11, 55.79), 'нижн': (44.0, 56.33), 'самар samara': (50.1, 53.2),
    'омск omsk': (73.37, 54.98), 'ростов rostov': (39.7, 47.23), 'уф ufa': (55.97, 54.73), 'красноярск krasnoyarsk': (92.87, 56.01),
    'перм perm': (56.25, 58.01), 'воронеж voronezh': (39.2, 51.66), 'волгоград volgograd': (44.5, 48.71), 'краснодар krasnodar': (38.98, 45.04),
    'саратов saratov': (46.03, 51.53), 'тюмен tyumen': (65.53, 57.15), 'ижевск': (53.2, 56.85), 'барнаул barnaul': (83.76, 53.35),
    'иркутск irkutsk': (104.28, 52.29), 'хабаровск khabarovsk': (135.07, 48.48), 'владивосток vladivostok': (131.89, 43.12),
    'ярослав': (39.89, 57.63), 'томск tomsk': (84.95, 56.49), 'оренбург': (55.1, 51.77), 'кемеров': (86.09, 55.35),
    'новокузнец': (87.12, 53.76), 'челябинск chelyabinsk': (61.4, 55.16), 'якутск yakutsk': (129.73, 62.03), 'мурманск murmansk': (33.08, 68.97),
    'калининград kaliningrad': (20.51, 54.71), 'сочи sochi': (39.73, 43.6), 'махачкал': (47.5, 42.98), 'ставропол': (41.97, 45.04),
    'тул tula': (37.62, 54.19), 'рязан': (39.74, 54.63), 'твер tver': (35.9, 56.86), 'архангельск': (40.54, 64.54),
    'сургут surgut': (73.4, 61.25), 'улан-удэ': (107.6, 51.83), 'чит chita': (113.5, 52.03), 'магадан': (150.8, 59.57),
    'петропавловск камчат': (158.65, 53.02), 'южно-сахалинск сахалин': (142.73, 46.96), 'благовещ': (127.53, 50.27),
    'абакан': (91.44, 53.72), 'кызыл': (94.45, 51.72), 'белгород': (36.58, 50.6), 'курск': (36.19, 51.73), 'пенз': (45.0, 53.2),
    'ульянов': (48.4, 54.32), 'киров': (49.67, 58.6), 'сыктывкар': (50.82, 61.67), 'вологд': (39.89, 59.22),
}
out = {'w': round(W), 'h': round(H), 'districts': {}, 'borders': ''.join(borders), 'cities': {}}
for k in DIST:
    lx, ly = px(proj(*labels[k]))
    out['districts'][k] = {'d': ''.join('M' + 'L'.join(f'{x:.0f},{y:.0f}' for x, y in r[:-1]) + 'Z' for r in simp[k]), 'lx': round(lx), 'ly': round(ly)}
for words, (lon, lat) in CITIES.items():
    x, y = px(proj(lon, lat))
    out['cities'][words] = [round(x), round(y)]
json.dump(out, open(OUT, 'w'), ensure_ascii=False, separators=(',', ':'))
print('size', len(json.dumps(out, separators=(',', ':'))), 'h', round(H), 'borders', len(borders))
