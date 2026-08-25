#!/usr/bin/env python3
"""Перевод уведомлений диалплана Asterisk с почты на CRM.

Применён на боевой АТС 25.08.2026 (7 правок). Оставлен в репозитории, потому
что `pjupd.pl` перегенерирует только `pjsip.conf`, а `extensions.conf` правится
руками — и при следующем восстановлении из бэкапа правку нужно будет повторить
ровно так же, а не по памяти.

🛑 Идемпотентен: повторный запуск ничего не меняет (искать нечего — старые
вызовы уже заменены). Без `--apply` печатает diff и ничего не пишет.

    python3 patch_dialplan.py            # показать, что изменится
    python3 patch_dialplan.py --apply    # применить (с бэкапом рядом)
    asterisk -rx "dialplan reload"       # 🛑 reload, не restart: рестарт рвёт разговоры
"""
import re, sys, shutil, datetime

PATH = "/etc/asterisk/extensions.conf"
MISSED = {"miss_call_cc": "cc", "miss_call_osd": "osd", "miss_call_yuro": "yuro"}
VOICEMAIL = {"order_ticket": "cc", "mess_rec_cc": "cc",
             "mess_rec_osd": "osd", "mess_rec_yuro": "yuro"}

NEW_MISSED = ("    same => n,system(/etc/_aster-scr/sc_notify_crm.sh missed {code} "
              "${{CHANNEL(linkedid)}} ${{UNIQUEID}} ${{CALLERID(num)}})")
NEW_VM = ('same  => n,MixMonitor(${{filename}},W(2),/etc/_aster-scr/sc_notify_crm.sh '
          'voicemail {code} ${{CHANNEL(linkedid)}} ${{UNIQUEID}} "${{SIDEA}}" "${{filename}}")')

src = open(PATH, encoding="utf-8").read().split("\n")
out, ctx, inserted, changed = [], "", set(), 0

for line in src:
    m = re.match(r"^\s*\[([^\]]+)\]", line)
    if m:
        ctx = m.group(1)
        out.append(line); continue

    if ctx in MISSED:
        # Три письма на каждый пропущенный заменяем одним вызовом CRM.
        if "sc_send_missed.sh" in line:
            if ctx not in inserted:
                out.append(NEW_MISSED.format(code=MISSED[ctx]))
                inserted.add(ctx); changed += 1
            continue
        if re.match(r"^\s*same\s*=>\s*n,Set\(email=", line):
            continue

    if ctx in VOICEMAIL and re.search(r"MixMonitor\(.*sc_send_ticket_", line):
        out.append(NEW_VM.format(code=VOICEMAIL[ctx])); changed += 1
        continue

    out.append(line)

new = "\n".join(out)
if "--apply" in sys.argv:
    shutil.copy2(PATH, PATH + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    open(PATH, "w", encoding="utf-8").write(new)
    print(f"применено правок: {changed}")
else:
    import difflib
    print("\n".join(difflib.unified_diff(src, out, "было", "стало", lineterm="", n=2)))
    print(f"\n[dry-run] правок будет: {changed}")
