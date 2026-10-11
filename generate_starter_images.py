"""Generate deterministic branded placeholder scenes; replace with approved media later."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent / "starter"
W, H = 720, 1280
BASE = "/usr/share/fonts/truetype/dejavu/DejaVuSans"
BRANDS = {
 "forwardkit": {
   "name": "FORWARDKIT DIGITAL", "background": "#07232A", "accent": "#6AE4C4",
   "slides": [
    ("CLEANING BUSINESS STARTER KIT", "START SMARTER", "Your business deserves a strong beginning."),
    ("MAKE A CLEARER PLAN", "LESS GUESSWORK", "Keep your setup and next steps organized."),
    ("EDITABLE MATERIALS", "SAVE TIME", "Reusable customer messages and useful templates."),
    ("WORKFLOW ESSENTIALS", "TRACK INQUIRIES", "Organize customer conversations more easily."),
    ("ONE DIGITAL KIT", "JUST $29", "Explore the Cleaning Business Starter Kit."),
    ("FORWARDKIT DIGITAL", "BUILD FORWARD", "forwardkitdigit.myshopify.com"),
   ]},
 "qualifiedleads": {
   "name": "QUALIFIEDLEADS-AI", "background": "#0B1530", "accent": "#94BDFF",
   "slides": [
    ("PROPERTY INTELLIGENCE", "LOOK DEEPER", "Every property has a story."),
    ("DATA TO DIRECTION", "RESEARCH MATTERS", "Context can change what comes next."),
    ("EVIDENCE FIRST", "BETTER WORKFLOWS", "An AI-assisted research and acquisition project."),
    ("REAL ESTATE AI", "HUMAN CONTROL", "Technology supports decisions, not replaces them."),
    ("VETERAN-OWNED PROJECT", "BUILT WITH PURPOSE", "Real estate tools under development."),
    ("QUALIFIEDLEADS-AI", "FOLLOW THE STORY", "qualifiedleads-ai.com"),
   ]}
}

def font(size, bold=False):
    return ImageFont.truetype(BASE + ("-Bold" if bold else "") + ".ttf", size)

def wrap(draw, txt, ft, max_width):
    result, current = [], ""
    for word in txt.split():
        trial = (current + " " + word).strip()
        if draw.textbbox((0, 0), trial, font=ft)[2] > max_width and current:
            result.append(current)
            current = word
        else:
            current = trial
    if current:
        result.append(current)
    return result

for brand, conf in BRANDS.items():
    directory = ROOT / brand
    directory.mkdir(parents=True, exist_ok=True)
    for idx, (tagline, heading, detail) in enumerate(conf["slides"], 1):
        image = Image.new("RGB", (W, H), conf["background"])
        draw = ImageDraw.Draw(image)
        for j in range(6):
            radius = 135 + j * 75
            draw.arc((W//2 - radius, 800 - radius, W//2 + radius, 800 + radius), 190, 352, fill=conf["accent"], width=2)
        draw.rounded_rectangle((38, 44, W-38, 105), radius=28, fill="#FFFFFF")
        draw.text((65, 63), conf["name"], font=font(23, True), fill="#1A2230")
        draw.text((46, 190), tagline, font=font(21, True), fill=conf["accent"])
        y = 260
        for line in wrap(draw, heading, font(54, True), 620):
            draw.text((42, y), line, font=font(54, True), fill="#FFFFFF")
            y += 76
        y += 10
        for line in wrap(draw, detail, font(29), 635):
            draw.text((43, y), line, font=font(29), fill="#E5ECF4")
            y += 48
        draw.line((42, 1198, 677, 1198), fill="#718596", width=2)
        draw.text((43, 1220), f"{idx:02d} / 06", font=font(20, True), fill="#E5ECF4")
        image.save(directory / f"scene-{idx:02d}.png", optimize=True)
print("Generated 12 starter scenes")
