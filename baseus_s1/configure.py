"""Interactive selector setup for an unmodified Android application."""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .android import AndroidBridge, PACKAGE, select_node
from .common import CameraError, write_json


def candidates(xml: str):
    choices = []
    seen = set()
    for node in ET.fromstring(xml).iter("node"):
        if node.get("package") != PACKAGE or node.get("enabled") != "true":
            continue
        attrs = {key: node.get(attribute, "") for key, attribute in
                 (("text", "text"), ("description", "content-desc"), ("resource_id", "resource-id"))}
        if not (attrs["text"] or attrs["description"] or node.get("clickable") == "true"):
            continue
        selectors = [{key: value} for key, value in attrs.items() if value]
        selectors.append({key: value for key, value in attrs.items() if value})
        for selector in selectors:
            try:
                point = select_node(xml, selector)
            except CameraError:
                continue
            if not point:
                continue
            # Multiple labels at the same coordinates need only one candidate.
            if point not in seen:
                seen.add(point)
                choices.append((selector, point))
            break
    return choices


def choose(bridge: AndroidBridge, title: str):
    while True:
        print("\n" + title)
        choices = candidates(bridge.ui_xml())
        for index, (selector, point) in enumerate(choices, 1):
            print(f"{index:3d}. {json.dumps(selector, ensure_ascii=False)}  @ {point}")
        answer = input("Номер элемента, r — обновить, q — отмена: ").strip().lower()
        if answer == "q":
            raise CameraError("Настройка отменена; config.json не изменён")
        if answer == "r":
            continue
        if answer.isdecimal() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1][0]
        print("Нужен номер из списка. Если кнопки нет, отмените настройку и выполните inspect.")


def configure_ui(config: dict, config_path: Path):
    bridge = AndroidBridge(config["android"], config["camera"]["sn"])
    bridge.check()
    print("Войдите в Baseus на планшете. Настроим путь от главного экрана до live view S1.")
    input("Когда готовы, нажмите Enter: ")
    bridge.stop()
    try:
        bridge.shell("input", "keyevent", "KEYCODE_WAKEUP")
        bridge.shell("wm", "dismiss-keyguard")
        bridge.shell("monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1")
        input("Дождитесь главного экрана со списком камер и нажмите Enter: ")
        steps = []
        while len(steps) < 6:
            selector = choose(bridge, "Выберите камеру S1 или следующий элемент на пути к её live view.")
            bridge.tap_selector(selector, time.monotonic() + 30)
            steps.append(selector)
            answer = input("Дождитесь перехода. Теперь видите живое видео нужной S1? [y/N]: ").strip().lower()
            if answer in {"y", "yes", "д", "да"}:
                break
        else:
            raise CameraError("Слишком много переходов; используйте ручную настройку open_live")
        capture = choose(bridge, "Выберите штатную кнопку фото/снимка (не запись, не галерею).")
        sn = input(f"Серийный номер S1 [{config['camera']['sn']}]: ").strip() or config["camera"]["sn"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", sn):
            raise CameraError("Нужен серийный номер S1 из букв/цифр/_/-; конфигурация не изменена")
        # Save only after all steps were supplied and explicitly confirmed by the user.
        config["camera"]["sn"] = sn
        config["android"]["open_live"] = steps
        config["android"]["capture_selector"] = capture
        write_json(config_path, config)
        print("Настройка сохранена. Проверьте снимок: python baseus_capture.py once")
    finally:
        bridge.stop()
