# 실행: Docker가 있는 머신에서 docker run --rm -v <dir>:/out -w /out python:3.12-slim sh -c "pip -q install pillow && python make-icon.py"
#       → /out/icon-192.png, icon-512.png, preview.png 를 app/static/ 으로 복사
#       FG=1 을 주면 배경 없는 전경(카드·반짝이)만 splash-fg.png(앱 스플래시)와
#       icon-any-192/512.png(manifest purpose "any" — Android 시작 화면용)로 만든다
# 앱 아이콘: 고해상도(2048)로 그린 뒤 축소해 안티에일리어싱. Android 마스크(원/스쿼클)에 잘리지 않게
# 핵심 요소는 가운데 약 70% 안에 둔다.
import math, os, urllib.request
from PIL import Image, ImageDraw, ImageFilter, ImageFont

N = 2048
FONT = "/tmp/bricolage.ttf"
urllib.request.urlretrieve(
    "https://github.com/google/fonts/raw/main/ofl/bricolagegrotesque/BricolageGrotesque%5Bopsz,wdth,wght%5D.ttf", FONT)

def lerp(a, b, t): return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

# 배경: 대각선 보라 그라디언트 + 오른쪽 위 은은한 빛
bg = Image.new("RGB", (N, N))
top, bot = (124, 92, 255), (70, 38, 214)
px = bg.load()
for y in range(N):
    for x in range(0, N):
        t = (x * .35 + y * .65) / N
        px[x, y] = lerp(top, bot, t)
glow = Image.new("L", (N, N), 0)
ImageDraw.Draw(glow).ellipse((N * .45, -N * .25, N * 1.25, N * .55), fill=110)
glow = glow.filter(ImageFilter.GaussianBlur(N * .12))
bg = Image.composite(Image.new("RGB", (N, N), (190, 170, 255)), bg, glow)
FG = os.environ.get("FG") == "1"
img = Image.new("RGBA", (N, N), (0, 0, 0, 0)) if FG else bg.convert("RGBA")

def card(w, h, r, fill, angle, cx, cy, shadow=True):
    global img
    layer = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    if shadow:
        sh = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle((box[0], box[1] + N * .03, box[2], box[3] + N * .03), r, fill=(25, 8, 90, 120))
        sh = sh.rotate(angle, center=(cx, cy), resample=Image.BICUBIC).filter(ImageFilter.GaussianBlur(N * .03))
        img = Image.alpha_composite(img, sh)
    ImageDraw.Draw(layer).rounded_rectangle(box, r, fill=fill)
    return layer, box

# 뒤 카드: 라임, 왼쪽으로 기울임
back, _ = card(N * .44, N * .54, N * .09, (200, 241, 105, 255), 14, N * .44, N * .50)
img = Image.alpha_composite(img, back.rotate(14, center=(N * .44, N * .50), resample=Image.BICUBIC))

# 앞 카드: 흰색, 오른쪽으로 기울임 + 글자와 파란 밑줄을 카드와 함께 회전
front, box = card(N * .46, N * .56, N * .095, (255, 255, 255, 255), -7, N * .545, N * .52)
d = ImageDraw.Draw(front)
f = ImageFont.truetype(FONT, int(N * .25))
# 축 순서가 폰트마다 달라 이름으로 찾아 넣는다
axes = f.get_variation_axes()
want = {b"Weight": 800, b"Optical size": 96, b"Width": 100, "Weight": 800, "Optical size": 96, "Width": 100}
f.set_variation_by_axes([want.get(a["name"], a["default"]) for a in axes])
cx, cy = N * .545, N * .485
d.text((cx, cy), "Aa", font=f, fill=(26, 13, 99, 255), anchor="mm")
# 파란 볼펜 밑줄 (살짝 물결)
pts = [(cx - N * .15 + i * N * .3 / 40, cy + N * .12 + math.sin(i / 40 * math.pi * 2.2) * N * .008) for i in range(41)]
d.line(pts, fill=(61, 110, 255, 255), width=int(N * .022), joint="curve")
for p in (pts[0], pts[-1]):
    r = N * .011
    d.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r), fill=(61, 110, 255, 255))
img = Image.alpha_composite(img, front.rotate(-7, center=(N * .545, N * .52), resample=Image.BICUBIC))

# 반짝이(AI): 오른쪽 위, 라임
def sparkle(cx, cy, R, fill):
    pts = []
    for i in range(8):
        a = math.pi / 4 * i - math.pi / 2
        r = R if i % 2 == 0 else R * .28
        pts.append((cx + math.cos(a) * r, cy + math.sin(a) * r))
    ImageDraw.Draw(img).polygon(pts, fill=fill)
sparkle(N * .745, N * .265, N * .085, (200, 241, 105, 255))
sparkle(N * .815, N * .385, N * .035, (255, 255, 255, 230))

if FG:
    img.crop((int(N * .12), int(N * .12), int(N * .88), int(N * .88))).resize((640, 640), Image.LANCZOS).save("/out/splash-fg.png", optimize=True)
    # manifest의 purpose "any" 아이콘 = 투명 배경. Android(Chrome)가 앱 시작 화면에 이 아이콘을
    # background_color(보라) 위에 그린다. 배경이 꽉 찬 아이콘을 쓰면 흰 틀에 끼워져 보인다.
    # 1024는 시작 화면용: Chrome은 아이콘이 작으면 "작은 아이콘 + 앱 이름" 레이아웃을 써서 흰 틀처럼 보인다.
    for s in (192, 512, 1024):
        img.resize((s, s), Image.LANCZOS).save(f"/out/icon-any-{s}.png", optimize=True)
    raise SystemExit
img = img.convert("RGB")
for s in (192, 512):
    img.resize((s, s), Image.LANCZOS).save(f"/out/icon-{s}.png", optimize=True)
# 미리보기: 원형 마스크와 스쿼클 마스크를 씌운 모습
prev = Image.new("RGB", (1100, 540), (240, 240, 245))
i5 = img.resize((500, 500), Image.LANCZOS)
m = Image.new("L", (500, 500), 0); ImageDraw.Draw(m).ellipse((0, 0, 499, 499), fill=255)
prev.paste(i5, (20, 20), m)
m2 = Image.new("L", (500, 500), 0); ImageDraw.Draw(m2).rounded_rectangle((0, 0, 499, 499), 130, fill=255)
prev.paste(i5, (580, 20), m2)
prev.save("/out/preview.png")
