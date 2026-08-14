from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math
import random
import base64
import os
import json

# ========== CONFIG ==========
W, H = 800, 600
TITLE = "小狐狸和萤火虫的森林之夜"
FONT_PATH = None
for fp in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "/System/Library/Fonts/PingFang.ttc"]:
    if os.path.exists(fp):
        FONT_PATH = fp
        break

def get_font(size):
    if FONT_PATH:
        try:
            return ImageFont.truetype(FONT_PATH, size)
        except:
            pass
    return ImageFont.load_default()

# ========== SCENE DATA ==========
scenes = [
    {
        "title": "第一幕",
        "narration": "夜幕降临，森林里的小狐狸独自徘徊。",
        "dialogue": "小狐狸：好安静啊……",
        "bg_top": (30, 40, 80), "bg_bottom": (10, 20, 40),
        "elements": ["moon", "stars", "trees"],
        "characters": [("fox", (255, 140, 50), "sad")],
        "bubble": (250, 250, 250)
    },
    {
        "title": "第二幕",
        "narration": "忽然，一点微光在草丛中闪烁。",
        "dialogue": "小狐狸：咦？那是什么？",
        "bg_top": (40, 50, 90), "bg_bottom": (15, 25, 50),
        "elements": ["moon", "stars", "trees", "fireflies"],
        "characters": [("fox", (255, 140, 50), "surprised")],
        "bubble": (250, 250, 200)
    },
    {
        "title": "第三幕",
        "narration": "萤火虫们从草丛中飞起，像星星一样闪耀。",
        "dialogue": "萤火虫：你好呀，小狐狸！",
        "bg_top": (50, 60, 100), "bg_bottom": (20, 30, 60),
        "elements": ["moon", "stars", "trees", "fireflies", "fireflies"],
        "characters": [("fox", (255, 140, 50), "happy"), ("firefly", (255, 255, 100), "happy")],
        "bubble": (250, 250, 150)
    },
    {
        "title": "第四幕",
        "narration": "萤火虫们围着小狐狸跳舞，森林变得明亮起来。",
        "dialogue": "小狐狸：你们真漂亮！",
        "bg_top": (60, 70, 110), "bg_bottom": (25, 35, 70),
        "elements": ["moon", "stars", "trees", "fireflies", "fireflies", "fireflies"],
        "characters": [("fox", (255, 140, 50), "happy"), ("firefly", (255, 255, 100), "happy")],
        "bubble": (250, 250, 200)
    },
    {
        "title": "第五幕",
        "narration": "小狐狸开心地跳起来，和萤火虫一起玩耍。",
        "dialogue": "小狐狸：哈哈哈，好开心！",
        "bg_top": (70, 80, 120), "bg_bottom": (30, 40, 80),
        "elements": ["moon", "stars", "trees", "fireflies", "fireflies", "fireflies", "fireflies"],
        "characters": [("fox", (255, 140, 50), "happy"), ("firefly", (255, 255, 100), "happy")],
        "bubble": (250, 250, 200)
    },
    {
        "title": "第六幕",
        "narration": "萤火虫们带着小狐狸来到森林深处。",
        "dialogue": "萤火虫：跟我来，有惊喜！",
        "bg_top": (80, 90, 130), "bg_bottom": (35, 45, 90),
        "elements": ["moon", "stars", "trees", "fireflies", "fireflies", "fireflies", "fireflies", "fireflies"],
        "characters": [("fox", (255, 140, 50), "surprised"), ("firefly", (255, 255, 100), "happy")],
        "bubble": (250, 250, 200)
    },
    {
        "title": "第七幕",
        "narration": "眼前是一片萤火虫的海洋，美得让人窒息。",
        "dialogue": "小狐狸：哇……太美了！",
        "bg_top": (90, 100, 140), "bg_bottom": (40, 50, 100),
        "elements": ["moon", "stars", "trees", "fireflies", "fireflies", "fireflies", "fireflies", "fireflies", "fireflies"],
        "characters": [("fox", (255, 140, 50), "amazed"), ("firefly", (255, 255, 100), "happy")],
        "bubble": (250, 250, 200)
    },
    {
        "title": "第八幕",
        "narration": "从此，小狐狸和萤火虫成了最好的朋友。",
        "dialogue": "小狐狸：谢谢你们，让我不再孤单。",
        "bg_top": (100, 110, 150), "bg_bottom": (50, 60, 110),
        "elements": ["moon", "stars", "trees", "fireflies", "fireflies", "fireflies", "fireflies", "fireflies", "fireflies", "fireflies"],
        "characters": [("fox", (255, 140, 50), "happy"), ("firefly", (255, 255, 100), "happy")],
        "bubble": (250, 250, 200)
    }
]

# ========== DRAWING HELPERS ==========
def draw_gradient(draw, top_color, bottom_color):
    for y in range(H):
        ratio = y / H
        r = int(top_color[0] * (1 - ratio) + bottom_color[0] * ratio)
        g = int(top_color[1] * (1 - ratio) + bottom_color[1] * ratio)
        b = int(top_color[2] * (1 - ratio) + bottom_color[2] * ratio)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

def draw_moon(draw, x, y, r):
    # 月亮
    draw.ellipse([x-r, y-r, x+r, y+r], fill=(255, 255, 200), outline=(255, 255, 255), width=2)
    # 光晕
    for i in range(3):
        alpha = 30 - i * 8
        draw.ellipse([x-r-i*8, y-r-i*8, x+r+i*8, y+r+i*8], outline=(255, 255, 200, alpha), width=2)

def draw_stars(draw, count=30):
    random.seed(42)
    for _ in range(count):
        x = random.randint(0, W)
        y = random.randint(0, H//2)
        size = random.randint(1, 3)
        brightness = random.randint(150, 255)
        draw.ellipse([x, y, x+size, y+size], fill=(brightness, brightness, brightness))

def draw_trees(draw, base_y=H-100):
    # 远处的树
    for i in range(5):
        x = 50 + i * 160
        h = random.randint(100, 180)
        # 树干
        draw.rectangle([x-10, base_y-h, x+10, base_y], fill=(60, 40, 20))
        # 树冠
        draw.ellipse([x-40, base_y-h-40, x+40, base_y-h+20], fill=(30, 80, 30))
        draw.ellipse([x-30, base_y-h-50, x+30, base_y-h-10], fill=(40, 100, 40))

def draw_fireflies(draw, count=10):
    random.seed(123)
    for _ in range(count):
        x = random.randint(50, W-50)
        y = random.randint(100, H-150)
        r = random.randint(2, 5)
        # 发光效果
        for i in range(3):
            alpha = 100 - i * 30
            draw.ellipse([x-r-i*4, y-r-i*4, x+r+i*4, y+r+i*4], fill=(255, 255, 100, alpha))
        draw.ellipse([x-r, y-r, x+r, y+r], fill=(255, 255, 200))

def draw_character(draw, char_type, color, expression, x, y, scale=1.0):
    # Q版角色
    body_r = int(30 * scale)
    head_r = int(25 * scale)
    
    # 身体
    draw.ellipse([x-body_r, y+20*scale, x+body_r, y+body_r+40*scale], fill=color, outline=(0, 0, 0), width=2)
    
    # 头
    draw.ellipse([x-head_r, y-head_r, x+head_r, y+head_r], fill=color, outline=(0, 0, 0), width=2)
    
    # 耳朵（狐狸）
    if char_type == "fox":
        draw.polygon([(x-20*scale, y-15*scale), (x-10*scale, y-40*scale), (x, y-20*scale)], fill=color, outline=(0, 0, 0))
        draw.polygon([(x+20*scale, y-15*scale), (x+10*scale, y-40*scale), (x, y-20*scale)], fill=color, outline=(0, 0, 0))
    
    # 眼睛
    eye_y = y - 5*scale
    if expression == "happy":
        # 弯弯的眼睛
        draw.arc([x-15*scale, eye_y-5*scale, x-5*scale, eye_y+5*scale], 0, 180, fill=(0, 0, 0), width=2)
        draw.arc([x+5*scale, eye_y-5*scale, x+15*scale, eye_y+5*scale], 0, 180, fill=(0, 0, 0), width=2)
    elif expression == "sad":
        # 下垂的眼睛
        draw.arc([x-15*scale, eye_y, x-5*scale, eye_y+10*scale], 180, 360, fill=(0, 0, 0), width=2)
        draw.arc([x+5*scale, eye_y, x+15*scale, eye_y+10*scale], 180, 360, fill=(0, 0, 0), width=2)
    elif expression == "surprised" or expression == "amazed":
        # 大眼睛
        draw.ellipse([x-15*scale, eye_y-5*scale, x-5*scale, eye_y+5*scale], fill=(255, 255, 255), outline=(0, 0, 0), width=2)
        draw.ellipse([x+5*scale, eye_y-5*scale, x+15*scale, eye_y+5*scale], fill=(255, 255, 255), outline=(0, 0, 0), width=2)
        draw.ellipse([x-12*scale, eye_y-2*scale, x-8*scale, eye_y+2*scale], fill=(0, 0, 0))
        draw.ellipse([x+8*scale, eye_y-2*scale, x+12*scale, eye_y+2*scale], fill=(0, 0, 0))
    else:
        # 普通眼睛
        draw.ellipse([x-12*scale, eye_y-3*scale, x-8*scale, eye_y+3*scale], fill=(0, 0, 0))
        draw.ellipse([x+8*scale, eye_y-3*scale, x+12*scale, eye_y+3*scale], fill=(0, 0, 0))
    
    # 嘴巴
    mouth_y = y + 10*scale
    if expression == "happy":
        draw.arc([x-10*scale, mouth_y-5*scale, x+10*scale, mouth_y+10*scale], 0, 180, fill=(0, 0, 0), width=2)
    elif expression == "sad":
        draw.arc([x-10*scale, mouth_y, x+10*scale, mouth_y+10*scale], 180, 360, fill=(0, 0, 0), width=2)
    elif expression == "surprised":
        draw.ellipse([x-5*scale, mouth_y-3*scale, x+5*scale, mouth_y+5*scale], fill=(0, 0, 0))
    elif expression == "amazed":
        draw.ellipse([x-8*scale, mouth_y-5*scale, x+8*scale, mouth_y+5*scale], fill=(0, 0, 0))
    else:
        draw.line([x-8*scale, mouth_y, x+8*scale, mouth_y], fill=(0, 0, 0), width=2)

def draw_bubble(draw, text, x, y, bubble_color=(255, 255, 255)):
    font = get_font(20)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 15
    bw, bh = tw + pad*2, th + pad*2
    
    # 气泡
    draw.ellipse([x-bw//2, y-bh//2, x+bw//2, y+bh//2], fill=bubble_color, outline=(0, 0, 0), width=2)
    # 尾巴
    draw.polygon([(x-10, y+bh//2), (x+10, y+bh//2), (x, y+bh//2+20)], fill=bubble_color, outline=(0, 0, 0))
    
    # 文字
    draw.text((x-tw//2, y-th//2), text, fill=(0, 0, 0), font=font)

# ========== RENDER SCENES ==========
frames = []
for idx, scene in enumerate(scenes):
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    
    # 背景渐变
    draw_gradient(draw, scene["bg_top"], scene["bg_bottom"])
    
    # 场景元素
    for elem in scene["elements"]:
        if elem == "moon":
            draw_moon(draw, W-100, 80, 40)
        elif elem == "stars":
            draw_stars(draw)
        elif elem == "trees":
            draw_trees(draw)
        elif elem == "fireflies":
            draw_fireflies(draw, random.randint(8, 15))
    
    # 角色
    char_positions = [(200, 350), (550, 350)]
    for i, (char_type, color, expr) in enumerate(scene["characters"]):
        x, y = char_positions[i]
        if char_type == "firefly":
            # 萤火虫角色（小一点）
            draw_character(draw, "firefly", color, expr, x, y, scale=0.6)
        else:
            draw_character(draw, char_type, color, expr, x, y)
    
    # 漫画边框
    draw.rectangle([5, 5, W-5, H-5], outline=(255, 255, 255), width=3)
    
    # 幕标题
    font_title = get_font(30)
    draw.text((20, 15), scene["title"], fill=(255, 255, 255), font=font_title)
    
    # 旁白字幕条
    font_narr = get_font(22)
    narr_bbox = draw.textbbox((0, 0), scene["narration"], font=font_narr)
    narr_w = narr_bbox[2] - narr_bbox[0]
    narr_y = H - 60
    draw.rectangle([20, narr_y, W-20, narr_y+40], fill=(0, 0, 0, 180))
    draw.text((W//2 - narr_w//2, narr_y+5), scene["narration"], fill=(255, 255, 255), font=font_narr)
    
    # 对白气泡
    draw_bubble(draw, scene["dialogue"], W//2, 150, scene["bubble"])
    
    # 保存
    path = f"frame_{idx+1:02d}.png"
    img.save(path)
    frames.append(path)
    print(f"Generated {path}")

# ========== GENERATE HTML ==========
def img_to_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

html_parts = []
html_parts.append(f"""<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <title>{TITLE}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            font-family: 'Microsoft YaHei', sans-serif;
        }}
        .container {{
            max-width: 900px;
            width: 90%;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 20px;
            padding: 30px;
            backdrop-filter: blur(10px);
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        }}
        h1 {{
            text-align: center;
            color: #fff;
            margin-bottom: 20px;
            font-size: 2em;
            text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.5);
        }}
        .frame-container {{
            position: relative;
            text-align: center;
            margin-bottom: 20px;
        }}
        .frame-container img {{
            max-width: 100%;
            border-radius: 10px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
            transition: opacity 0.3s ease;
        }}
        .dialogue {{
            text-align: center;
            color: #fff;
            font-size: 1.2em;
            margin: 15px 0;
            padding: 15px;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 10px;
            min-height: 50px;
        }}
        .controls {{
            display: flex;
            justify-content: center;
            gap: 20px;
            margin-top: 20px;
        }}
        .controls button {{
            padding: 12px 30px;
            font-size: 1.1em;
            border: none;
            border-radius: 25px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            cursor: pointer;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
        }}
        .controls button:hover {{
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(102, 126, 234, 0.6);
        }}
        .controls button:active {{
            transform: translateY(0);
        }}
        .progress {{
            text-align: center;
            color: #aaa;
            margin-top: 10px;
        }}
        .auto-play {{
            text-align: center;
            margin-top: 10px;
        }}
        .auto-play label {{
            color: #aaa;
            cursor: pointer;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🦊 {TITLE} ✨</h1>
        <div class="frame-container">
            <img id="frame" src="data:image/png;base64,{img_to_base64(frames[0])}" alt="Frame">
        </div>
        <div class="dialogue" id="dialogue">{scenes[0]["dialogue"]}</div>
        <div class="controls">
            <button onclick="prevFrame()">⬅ 上一幕</button>
            <button onclick="nextFrame()">下一幕 ➡</button>
        </div>
        <div class="progress" id="progress">1 / {len(scenes)}</div>
        <div class="auto-play">
            <label><input type="checkbox" id="autoPlay" onchange="toggleAutoPlay()"> 自动播放</label>
        </div>
    </div>
    
    <script>
        const scenes = __SCENES_JSON__;
        let current = 0;
        let autoPlayTimer = null;
        
        function updateFrame() {{
            const frame = document.getElementById('frame');
            const dialogue = document.getElementById('dialogue');
            const progress = document.getElementById('progress');
            
            frame.style.opacity = '0';
            setTimeout(() => {{
                frame.src = 'data:image/png;base64,' + scenes[current].img;
                frame.style.opacity = '1';
            }}, 200);
            
            dialogue.textContent = scenes[current].dialogue;
            progress.textContent = (current + 1) + ' / ' + scenes.length;
        }}
        
        function nextFrame() {{
            if (current < scenes.length - 1) {{
                current++;
                updateFrame();
            }}
        }}
        
        function prevFrame() {{
            if (current > 0) {{
                current--;
                updateFrame();
            }}
        }}
        
        function toggleAutoPlay() {{
            const autoPlay = document.getElementById('autoPlay');
            if (autoPlay.checked) {{
                autoPlayTimer = setInterval(() => {{
                    if (current < scenes.length - 1) {{
                        nextFrame();
                    }} else {{
                        current = 0;
                        updateFrame();
                    }}
                }}, 4000);
            }} else {{
                clearInterval(autoPlayTimer);
            }}
        }}
        
        document.addEventListener('keydown', (e) => {{
            if (e.key === 'ArrowRight') nextFrame();
            if (e.key === 'ArrowLeft') prevFrame();
        }});
    </script>
</body>
</html>""")

html_content = "".join(html_parts)
html_content = html_content.replace(
    "__SCENES_JSON__",
    json.dumps([{"img": img_to_base64(f), "dialogue": s["dialogue"]} for f, s in zip(frames, scenes)]),
)
with open("manga.html", "w", encoding="utf-8") as f:
    f.write(html_content)

print(f"SUCCESS:SCENES:{len(scenes)}")