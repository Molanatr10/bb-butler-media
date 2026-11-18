"""Deterministic football result cards; no image-generation API or paid service."""
import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps


def font(size, bold=False):
    names = ('DejaVuSans-Bold.ttf', 'DejaVuSans.ttf') if bold else ('DejaVuSans.ttf',)
    roots = (Path('/usr/share/fonts/truetype/dejavu'), Path('C:/Windows/Fonts'))
    for root in roots:
        for name in (*names, 'arialbd.ttf' if bold else 'arial.ttf'):
            try:
                return ImageFont.truetype(str(root / name), size)
            except OSError:
                pass
    return ImageFont.load_default(size=size)


def fitted(draw, text, size, width, bold=False):
    while size > 16:
        face = font(size, bold)
        if draw.textlength(text, font=face) <= width:
            return face
        size -= 1
    return font(size, bold)


def render_card(data, badges=(None, None)):
    teams = data['teams']
    rows = max(len(team.get('goals', [])) for team in teams)
    spacing = 88 if any(goal.get('assist') for team in teams for goal in team.get('goals', [])) else 62
    height = max(720, 375 + rows * spacing + 100)
    canvas = Image.new('RGB', (1280, height))
    pixels = canvas.load()
    for y in range(height):
        for x in range(1280):
            wine = max(0, 1 - math.hypot(x / 1100, y / 800))
            gold = max(0, 1 - math.hypot((1280 - x) / 1200, y / 900))
            pixels[x, y] = (int(14 + 44 * wine + 22 * gold), int(30 + 7 * wine + 10 * gold), int(18 + 8 * wine))
    canvas = canvas.filter(ImageFilter.GaussianBlur(6))
    draw = ImageDraw.Draw(canvas)
    white, muted, green = '#f8faf6', '#a7b09f', '#4aca65'
    for index, team in enumerate(teams):
        cx = 180 if index == 0 else 1100
        badge = badges[index]
        if badge:
            badge = badge.convert('RGBA')
            if team.get('round_badge'):
                badge = ImageOps.fit(badge,(180,180),method=Image.Resampling.LANCZOS)
                mask=Image.new('L',(180,180)); ImageDraw.Draw(mask).ellipse((0,0,179,179),fill=255)
                badge.putalpha(mask)
            else:badge.thumbnail((180, 180))
            canvas.paste(badge, (cx - badge.width // 2, 48 + (180 - badge.height) // 2), badge)
        else:
            draw.ellipse((cx - 82, 55, cx + 82, 219), fill='#294336', outline='#4c6855', width=2)
            initials = ''.join(word[0] for word in team['name'].split()[:3]).upper()
            draw.text((cx, 137), initials, font=font(42, True), fill=white, anchor='mm')
        face = fitted(draw, team['name'], 44, 370, True)
        draw.text((cx, 269), team['name'], font=face, fill=white, anchor='mm')
    score = f"{teams[0]['score']} - {teams[1]['score']}"
    draw.text((640, 141), score, font=fitted(draw, score, 104, 430, True), fill=white, anchor='mm')
    draw.text((640, 238), data['status'], font=font(42), fill=white, anchor='mm')
    subtitle = data.get('competition', '')
    if data.get('penalties'):
        subtitle = 'Penalties: ' + data['penalties']
    draw.text((640, 308), subtitle, font=fitted(draw, subtitle, 22, 900), fill=muted, anchor='mm')
    # Draw the central ball instead of relying on an emoji font on the server.
    cy = 397
    draw.ellipse((614, cy - 26, 666, cy + 26), fill=green)
    points = [(640 + 12 * math.cos(-math.pi / 2 + i * math.tau / 5), cy + 12 * math.sin(-math.pi / 2 + i * math.tau / 5)) for i in range(5)]
    draw.polygon(points, fill='#183a24')
    for px, py in points:
        draw.line((px, py, 640 + (px - 640) * 2, cy + (py - cy) * 2), fill='#183a24', width=3)
    for index, team in enumerate(teams):
        start, end = (58, 580) if index == 0 else (704, 1226)
        goals = team.get('goals', [])
        for row, goal in enumerate(goals):
            y = 380 + row * spacing
            text = f"{goal['player']} {goal['minute']}" + (' (OG)' if goal.get('own_goal') else ' (P)' if goal.get('penalty') else '')
            draw.text((start, y), text, font=fitted(draw, text, 32, end - start), fill=muted)
            if goal.get('assist'):
                # Small boot-shaped assist icon.
                draw.polygon([(start, y+48), (start+8, y+41), (start+15, y+48), (start+30, y+53), (start+30, y+61), (start, y+61)], fill=green)
                text = 'Assist: ' + goal['assist']
                draw.text((start + 42, y + 42), text, font=fitted(draw, text, 22, end - start - 42), fill=muted)
        if not goals:
            draw.text((start, 390), 'Scorers pending' if team['score'] else 'No goals', font=font(28), fill=muted)
        cards = team.get('cards', [])
        if cards:
            text = '  ·  '.join(cards)
            draw.text((start, height - 63), text, font=fitted(draw, text, 20, end-start), fill=muted)
    output = io.BytesIO(); canvas.save(output, format='PNG', optimize=True)
    return output.getvalue()
