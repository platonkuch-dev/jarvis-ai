"""Direct scripting control of Adobe After Effects via adobe_cep_bridge --
exact, non-visual control where the CEP extension's ExtendScript bridge can
do it, instead of clicking through the UI (tools/computer_use.py).

Every op below sends a small, fixed ExtendScript template with validated
arguments substituted in -- never a script the LLM wrote itself, same
whitelist-only principle as tools/adobe_photoshop.py's run_action and
tools/dev.py's run_predefined_script.

Requires the one-time bridge setup: adobe_cep_bridge.installer.ensure_installed()
(copies the CEP extension + enables PlayerDebugMode) and then, once per
machine, opening Window > Extensions > Jarvis Bridge inside After Effects
itself -- CEP only opens the debug port once the panel has been instantiated
at least once, but After Effects remembers that panel across restarts
afterward, so this is a one-time step, not a per-launch one.
"""

from __future__ import annotations

import json
from typing import Literal

from livekit.agents import RunContext, function_tool

import config
from adobe_cep_bridge import AFTER_EFFECTS_PORT, client
from tools._logging import log_call
from tools.registry import register_impl, register_tool


def _jsx_string(s: str) -> str:
    """Safely embeds a Python string as an ExtendScript string literal."""
    return json.dumps(s)


# ExtendScript has no global JSON object (verified live -- typeof JSON is
# "undefined" there), so multi-value results are returned as delimited
# strings instead of JSON.stringify(...). Comp/layer names are free-form
# user text and can legitimately contain "|" or ":" (e.g. "Title | Scene
# 1"), which used to silently corrupt naive split("|")/split("::") parsing
# below -- sometimes raising "too many values to unpack", sometimes just
# mis-splitting a display string. These are ASCII control characters that
# cannot appear in an After Effects item/layer name, so they're safe
# delimiters no user-chosen name can collide with.
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


def _jsx_field_sep() -> str:
    return _jsx_string(_FIELD_SEP)


def _jsx_record_sep() -> str:
    return _jsx_string(_RECORD_SEP)


async def _eval(script: str) -> str:
    return await client.eval_script(AFTER_EFFECTS_PORT, script)


def _parse_color_hex(color_hex: str) -> tuple[float, float, float]:
    """Parses a "#RGB"/"#RRGGBB"-style hex color into 0-1 floats, with a
    friendly error instead of a raw ValueError from int(..., 16) on bad
    input (too short, non-hex characters)."""
    color_hex = color_hex.strip().lstrip("#")
    if len(color_hex) == 3:
        color_hex = "".join(c * 2 for c in color_hex)
    if len(color_hex) != 6 or any(c not in "0123456789abcdefABCDEF" for c in color_hex):
        raise ValueError(f"Некорректный hex-цвет: «{color_hex}» (ожидается формат RRGGBB, например FF0000).")
    r = int(color_hex[0:2], 16) / 255.0
    g = int(color_hex[2:4], 16) / 255.0
    b = int(color_hex[4:6], 16) / 255.0
    return r, g, b


async def _get_info() -> str:
    result = await _eval(f"""
        (function() {{
            var proj = app.project;
            var active = proj.activeItem;
            var file = proj.file ? proj.file.fsName : "";
            var activeName = active ? active.name : "";
            return file + {_jsx_field_sep()} + proj.items.length + {_jsx_field_sep()} + activeName;
        }})();
    """)
    file, item_count, active_name = result.split(_FIELD_SEP, 2)
    lines = [
        f"Проект: {file or '(не сохранён)'}",
        f"Элементов в проекте: {item_count}",
        f"Активный элемент: {active_name or '(нет)'}",
    ]
    return "\n".join(lines)


async def _create_composition(name: str, width: int, height: int, duration: float, frame_rate: float) -> str:
    if width <= 0 or height <= 0:
        raise ValueError("Ширина и высота композиции должны быть положительными.")
    if duration <= 0:
        raise ValueError("Длительность композиции должна быть положительной.")
    if frame_rate <= 0:
        raise ValueError("Частота кадров должна быть положительной.")
    result = await _eval(f"""
        (function() {{
            var comp = app.project.items.addComp({_jsx_string(name)}, {int(width)}, {int(height)}, 1, {float(duration)}, {float(frame_rate)});
            return comp.name;
        }})();
    """)
    return f"Композиция создана: {result} ({width}x{height}, {duration}с, {frame_rate}fps)"


async def _find_comp_snippet(comp_name: str) -> str:
    """ExtendScript snippet (no trailing semicolon) that resolves to the
    named composition or throws a friendly error -- shared by every op that
    needs to act on an existing comp by name."""
    return f"""
        (function() {{
            for (var i = 1; i <= app.project.items.length; i++) {{
                var it = app.project.items[i];
                if (it instanceof CompItem && it.name === {_jsx_string(comp_name)}) return it;
            }}
            throw new Error("Композиция не найдена: {comp_name}");
        }})()
    """


async def _add_text_layer(comp_name: str, text: str, font_size: int) -> str:
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layers.addText({_jsx_string(text)});
            var doc = layer.property("Source Text").value;
            doc.fontSize = {int(font_size)};
            layer.property("Source Text").setValue(doc);
            return layer.name;
        }})();
    """)
    return f"Текстовый слой добавлен в «{comp_name}»: {result}"


async def _add_solid_layer(comp_name: str, name: str, color_hex: str) -> str:
    r, g, b = _parse_color_hex(color_hex)
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layers.addSolid([{r}, {g}, {b}], {_jsx_string(name)}, comp.width, comp.height, comp.pixelAspect, comp.duration);
            return layer.name;
        }})();
    """)
    return f"Слой-заливка добавлен в «{comp_name}»: {result}"


def _js_value(value) -> str:
    """Renders a Python number/string/list as an ExtendScript literal."""
    if isinstance(value, str):
        return _jsx_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_js_value(x) for x in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(float(value))


# AE's own "Easy Ease" (F9) uses 33.3% influence on both sides -- matching
# that as the "ease" default keeps output looking like something a human
# editor would reach for, not an arbitrary made-up curve.
_EASE_INFLUENCE = {
    "linear": None,               # no bezier conversion -- straight interpolation
    "ease": (33.3, 33.3),         # smooth in and out, like AE's Easy Ease
    "ease_in": (66.0, 0.001),     # decelerates into this keyframe, linear leaving it
    "ease_out": (0.001, 66.0),    # linear arriving, accelerates away from this keyframe
}


async def _animate_property(
    comp_name: str, layer_name: str, property_name: str, keyframes: list[dict],
) -> str:
    if not keyframes:
        raise ValueError("Список keyframes пуст.")

    set_calls = []
    ease_calls = []
    for i, kf in enumerate(keyframes):
        if not isinstance(kf, dict) or "time" not in kf or "value" not in kf:
            raise ValueError(f"keyframes[{i}] должен быть объектом с полями 'time' и 'value'.")
        ease = kf.get("ease", "linear")
        if ease not in _EASE_INFLUENCE:
            raise ValueError(
                f"keyframes[{i}]: неизвестный ease «{ease}» (linear|ease|ease_in|ease_out)."
            )
        t = float(kf["time"])
        set_calls.append(f"prop.setValueAtTime({t}, {_js_value(kf['value'])});")
        influence = _EASE_INFLUENCE[ease]
        if influence is None:
            continue
        in_infl, out_infl = influence
        # setTemporalEaseAtKey's expected array length depends on whether
        # the property is spatial, not on prop.value.length directly
        # (verified live): a *spatial* property like Position moves along a
        # single motion-path curve, so it takes exactly ONE KeyframeEase per
        # side regardless of how many axes it has. A non-spatial vector
        # property like Scale animates each component on its own curve --
        # and is internally 3D ([x, y, z]) even in a 2D comp -- so it needs
        # one KeyframeEase per component (3 for Scale, 1 for Opacity/Rotation).
        ease_calls.append(f"""
            (function() {{
                var idx = prop.nearestKeyIndex({t});
                prop.setInterpolationTypeAtKey(idx, KeyframeInterpolationType.BEZIER, KeyframeInterpolationType.BEZIER);
                var dim = prop.isSpatial ? 1 : ((prop.value instanceof Array) ? prop.value.length : 1);
                var easeIn = [], easeOut = [];
                for (var d = 0; d < dim; d++) {{
                    easeIn.push(new KeyframeEase(0, {in_infl}));
                    easeOut.push(new KeyframeEase(0, {out_infl}));
                }}
                prop.setTemporalEaseAtKey(idx, easeIn, easeOut);
            }})();
        """)

    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layer({_jsx_string(layer_name)});
            var prop = layer.property({_jsx_string(property_name)});
            {"".join(set_calls)}
            {"".join(ease_calls)}
            return layer.name + {_jsx_field_sep()} + prop.name + {_jsx_field_sep()} + prop.numKeys;
        }})();
    """)
    layer, prop_name, num_keys = result.split(_FIELD_SEP)
    return f"Анимация «{prop_name}» на слое «{layer}»: {num_keys} ключевых кадров."


# Effect (and per-effect parameter) *display* names are localized -- e.g. on
# a Russian After Effects, addProperty("Gaussian Blur") and
# fx.property("Blurriness") both fail (verified live: the real property name
# was "Размытость"). Match names are locale-independent and stable across AE
# versions, so this maps the common English names an LLM would naturally use
# to their real match names; anything already looking like a match name
# ("ADBE ...") or not in this table is passed through as-is (works verbatim
# on an English-locale AE, or if the caller already knows the match name).
_EFFECT_MATCH_NAMES = {
    "gaussian blur": "ADBE Gaussian Blur 2",
    "glow": "ADBE Glo2",
    "drop shadow": "ADBE Drop Shadow",
    "hue/saturation": "ADBE HUE SATURATION",
    "hue saturation": "ADBE HUE SATURATION",
    "curves": "ADBE CurvesCustom",
    "fill": "ADBE Fill",
    "tint": "ADBE Tint",
    "basic 3d": "ADBE Basic 3D",
}

_EFFECT_PARAM_MATCH_NAMES = {
    "ADBE Gaussian Blur 2": {
        "blurriness": "ADBE Gaussian Blur 2-0001",
        "blur dimensions": "ADBE Gaussian Blur 2-0002",
        "repeat edge pixels": "ADBE Gaussian Blur 2-0003",
    },
    "ADBE Drop Shadow": {
        "shadow color": "ADBE Drop Shadow-0001",
        "opacity": "ADBE Drop Shadow-0002",
        "direction": "ADBE Drop Shadow-0003",
        "distance": "ADBE Drop Shadow-0004",
        "softness": "ADBE Drop Shadow-0005",
        "shadow only": "ADBE Drop Shadow-0006",
    },
    "ADBE Glo2": {
        "glow based on": "ADBE Glo2-0001",
        "glow threshold": "ADBE Glo2-0002",
        "glow radius": "ADBE Glo2-0003",
        "glow intensity": "ADBE Glo2-0004",
    },
    "ADBE HUE SATURATION": {
        "channel range": "ADBE HUE SATURATION-0003",
        "master hue": "ADBE HUE SATURATION-0004",
        "master saturation": "ADBE HUE SATURATION-0005",
        "master lightness": "ADBE HUE SATURATION-0006",
    },
}


def _resolve_effect_match_name(effect_name: str) -> str:
    return _EFFECT_MATCH_NAMES.get(effect_name.strip().lower(), effect_name)


def _resolve_effect_param_match_name(effect_match_name: str, param_name: str) -> str:
    table = _EFFECT_PARAM_MATCH_NAMES.get(effect_match_name, {})
    return table.get(param_name.strip().lower(), param_name)


async def _add_effect(comp_name: str, layer_name: str, effect_name: str, params: dict) -> str:
    match_name = _resolve_effect_match_name(effect_name)
    # Each param set is wrapped in its own try/catch so one bad key doesn't
    # take down the whole call -- the effect itself was already added
    # successfully by that point, and failures are reported back by name.
    param_calls = "\n".join(f"""
        try {{
            effect.property({_jsx_string(_resolve_effect_param_match_name(match_name, k))}).setValue({_js_value(v)});
        }} catch (e) {{ failed.push({_jsx_string(k)}); }}
    """ for k, v in params.items())
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layer({_jsx_string(layer_name)});
            var effect = layer.property("ADBE Effect Parade").addProperty({_jsx_string(match_name)});
            var failed = [];
            {param_calls}
            return layer.name + {_jsx_field_sep()} + effect.name + {_jsx_field_sep()} + failed.join({_jsx_record_sep()});
        }})();
    """)
    layer, effect, failed = result.split(_FIELD_SEP)
    message = f"Эффект «{effect}» добавлен на слой «{layer}»."
    if failed:
        message += f" Не удалось установить параметры: {failed.split(_RECORD_SEP)}."
    return message


_SHAPE_SIZE_PROP = {
    "rectangle": "ADBE Vector Shape - Rect",
    "ellipse": "ADBE Vector Shape - Ellipse",
}
_SHAPE_SIZE_PARAM = {
    "rectangle": "ADBE Vector Rect Size",
    "ellipse": "ADBE Vector Ellipse Size",
}


async def _add_shape_layer(
    comp_name: str, shape_type: str, name: str, width: float, height: float,
    color_hex: str, x: float, y: float,
) -> str:
    if shape_type not in _SHAPE_SIZE_PROP:
        raise ValueError(f"Неизвестный тип фигуры: {shape_type} (rectangle|ellipse)")
    if width <= 0 or height <= 0:
        raise ValueError("Ширина и высота фигуры должны быть положительными.")
    r, g, b = _parse_color_hex(color_hex)
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layers.addShape();
            layer.name = {_jsx_string(name)};
            var contents = layer.property("ADBE Root Vectors Group");
            var group = contents.addProperty("ADBE Vector Group");
            var groupContents = group.property("ADBE Vectors Group");
            var shape = groupContents.addProperty({_jsx_string(_SHAPE_SIZE_PROP[shape_type])});
            shape.property({_jsx_string(_SHAPE_SIZE_PARAM[shape_type])}).setValue([{float(width)}, {float(height)}]);
            var fill = groupContents.addProperty("ADBE Vector Graphic - Fill");
            fill.property("ADBE Vector Fill Color").setValue([{r}, {g}, {b}]);
            layer.property("Position").setValue([{float(x)}, {float(y)}]);
            return layer.name;
        }})();
    """)
    return f"Слой-фигура ({shape_type}) добавлен в «{comp_name}»: {result}"


async def _add_footage_layer(comp_name: str, path: str) -> str:
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var f = new File({_jsx_string(path)});
            if (!f.exists) return "NOT_FOUND";
            var footageItem = null;
            for (var i = 1; i <= app.project.items.length; i++) {{
                var it = app.project.items[i];
                if (it instanceof FootageItem && it.mainSource && it.mainSource.file
                    && it.mainSource.file.fsName === f.fsName) {{ footageItem = it; break; }}
            }}
            if (!footageItem) {{
                footageItem = app.project.importFile(new ImportOptions(f));
            }}
            var comp = {comp_expr};
            var layer = comp.layers.add(footageItem);
            return layer.name;
        }})();
    """)
    if result == "NOT_FOUND":
        return f"Файл не найден: {path}"
    return f"Футаж добавлен в «{comp_name}» как слой: {result}"


async def _set_expression(comp_name: str, layer_name: str, property_name: str, expression: str) -> str:
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layer({_jsx_string(layer_name)});
            var prop = layer.property({_jsx_string(property_name)});
            prop.expression = {_jsx_string(expression)};
            return prop.name + {_jsx_field_sep()} + (prop.expressionError || "");
        }})();
    """)
    prop_name, error = result.split(_FIELD_SEP, 1)
    if error:
        raise RuntimeError(f"Expression отклонён After Effects: {error}")
    return f"Expression установлен на «{prop_name}» (слой «{layer_name}»)."


async def _add_to_render_queue(comp_name: str, output_path: str) -> str:
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var item = app.project.renderQueue.items.add(comp);
            var outModule = item.outputModule(1);
            outModule.file = new File({_jsx_string(output_path)});
            return "queued: " + comp.name + " -> " + outModule.file.fsName;
        }})();
    """)
    return f"Добавлено в очередь рендеринга: {result}"


async def _render() -> str:
    # q.render() blocks ExtendScript (and AE's main thread) until every
    # queued item finishes -- the CDP round-trip that reaches it goes over
    # that same main thread, so a render with any real content routinely
    # takes well past the default 20s eval_script timeout (measured live:
    # a 4s/five-layer/two-effect comp alone needed ~40s). Give it much more
    # room than everything else in this module.
    #
    # q.render() itself never throws for a per-item failure (missing codec,
    # disk full, bad output path) -- it just marks that item's status and
    # moves on, so the old "rendered N item(s)" message reported success
    # even when every item had actually failed. Check each item's status
    # afterward and surface failures by comp name instead of staying silent.
    result = await client.eval_script(AFTER_EFFECTS_PORT, f"""
        (function() {{
            var q = app.project.renderQueue;
            if (q.numItems === 0) return "empty";
            var total = q.numItems;
            q.render();
            var failed = [];
            for (var i = 1; i <= total; i++) {{
                var it = q.item(i);
                if (it.status === RQItemStatus.ERR_STOPPED) failed.push(it.comp.name);
            }}
            return total + {_jsx_field_sep()} + failed.join({_jsx_record_sep()});
        }})();
    """, timeout=600.0)
    if result == "empty":
        return "Очередь рендеринга пуста -- сначала добавьте композицию (add_to_render_queue)."
    total, failed_raw = result.split(_FIELD_SEP, 1)
    failed = failed_raw.split(_RECORD_SEP) if failed_raw else []
    if failed:
        return f"Рендеринг завершён: {total} элемент(ов), из них с ошибкой: {', '.join(failed)}."
    return f"Рендеринг запущен и завершён: {total} элемент(ов)."


async def _save_project() -> str:
    result = await _eval("""
        (function() {
            if (!app.project.file) return "unsaved";
            app.project.save();
            return app.project.file.fsName;
        })();
    """)
    if result == "unsaved":
        return "Проект ещё не имеет имени файла -- сохраните его вручную один раз (Файл > Сохранить как), дальше можно будет через save."
    return f"Проект сохранён: {result}"


async def _save_project_as(path: str) -> str:
    result = await _eval(f"""
        (function() {{
            var f = new File({_jsx_string(path)});
            app.project.save(f);
            return app.project.file.fsName;
        }})();
    """)
    return f"Проект сохранён как: {result}"


async def _open_project(path: str, discard_unsaved: bool) -> str:
    # app.open() shows AE's own native "Save changes?" dialog whenever the
    # current project is dirty -- that's a MODAL, so the ExtendScript call
    # (and the CDP round-trip waiting on it) blocks until a human clicks it,
    # which reliably blows through eval_script's timeout (verified live: a
    # call just hangs there with no error until someone at the keyboard
    # resolves the dialog). Checking .dirty first avoids ever triggering it.
    dirty = await _eval("app.project.dirty ? \"DIRTY\" : \"CLEAN\";")
    if dirty == "DIRTY" and not discard_unsaved:
        return (
            "В текущем проекте есть несохранённые изменения -- сохраните их (save/save_as) "
            "или повторите вызов с discard_unsaved=true, чтобы закрыть без сохранения."
        )
    if dirty == "DIRTY":
        await _eval("app.project.close(CloseOptions.DO_NOT_SAVE_CHANGES);")

    result = await _eval(f"""
        (function() {{
            var f = new File({_jsx_string(path)});
            if (!f.exists) return "NOT_FOUND";
            app.open(f);
            return app.project.file ? app.project.file.fsName : "opened";
        }})();
    """)
    if result == "NOT_FOUND":
        return f"Файл не найден: {path}"
    return f"Проект открыт: {result}"


async def _list_compositions() -> str:
    # Discovery step for working inside a project Джарвис didn't just
    # create itself -- every other action needs a comp/layer *name* to act
    # on, so this (and _list_layers below) is how it finds out what's
    # actually there instead of guessing.
    result = await _eval(f"""
        (function() {{
            var out = [];
            for (var i = 1; i <= app.project.items.length; i++) {{
                var it = app.project.items[i];
                if (it instanceof CompItem) {{
                    out.push(it.name + {_jsx_field_sep()} + it.width + "x" + it.height + {_jsx_field_sep()}
                        + it.duration.toFixed(2) + "s" + {_jsx_field_sep()} + it.frameRate.toFixed(2)
                        + "fps" + {_jsx_field_sep()} + it.numLayers + " layers");
                }}
            }}
            return out.join({_jsx_record_sep()});
        }})();
    """)
    if not result:
        return "В проекте нет композиций."
    lines = [f"- {'; '.join(item.split(_FIELD_SEP))}" for item in result.split(_RECORD_SEP)]
    return "Композиции в проекте:\n" + "\n".join(lines)


_LAYER_KIND_JS = """
    function layerKind(l) {
        if (l instanceof TextLayer) return "text";
        if (l instanceof CameraLayer) return "camera";
        if (l instanceof LightLayer) return "light";
        if (l instanceof ShapeLayer) return "shape";
        if (l.nullLayer) return "null";
        if (l.source instanceof CompItem) return "precomp";
        return "layer";
    }
"""


async def _list_layers(comp_name: str) -> str:
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            {_LAYER_KIND_JS}
            var comp = {comp_expr};
            var out = [];
            for (var i = 1; i <= comp.numLayers; i++) {{
                var l = comp.layer(i);
                out.push(i + {_jsx_field_sep()} + l.name + {_jsx_field_sep()} + layerKind(l) + {_jsx_field_sep()} + (l.threeDLayer ? "3D" : "2D"));
            }}
            return out.join({_jsx_record_sep()});
        }})();
    """)
    if not result:
        return f"В композиции «{comp_name}» нет слоёв."
    lines = [f"- {'; '.join(item.split(_FIELD_SEP))}" for item in result.split(_RECORD_SEP)]
    return f"Слои в «{comp_name}»:\n" + "\n".join(lines)


async def _precompose(comp_name: str, layer_names: list[str], new_comp_name: str, move_all: bool) -> str:
    if not layer_names:
        raise ValueError("Список layer_names пуст.")
    names_js = "[" + ", ".join(_jsx_string(n) for n in layer_names) + "]"
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var names = {names_js};
            var indices = [];
            for (var n = 0; n < names.length; n++) {{
                var layer = comp.layer(names[n]);
                if (!layer) throw new Error("Слой не найден: " + names[n]);
                indices.push(layer.index);
            }}
            var newComp = comp.layers.precompose(indices, {_jsx_string(new_comp_name)}, {str(move_all).lower()});
            return newComp.name;
        }})();
    """)
    return f"Слои {layer_names} объединены в новую композицию «{result}»."


async def _set_3d(comp_name: str, layer_name: str, enabled: bool) -> str:
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layer({_jsx_string(layer_name)});
            layer.threeDLayer = {str(enabled).lower()};
            return layer.name + {_jsx_field_sep()} + layer.threeDLayer;
        }})();
    """)
    layer, state = result.split(_FIELD_SEP)
    return f"3D для слоя «{layer}»: {'включено' if state == 'true' else 'выключено'}."


async def _add_camera(comp_name: str, name: str, position: list[float] | None) -> str:
    comp_expr = await _find_comp_snippet(comp_name)
    pos_call = f'cam.property("Position").setValue({_js_value(position)});' if position else ""
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var cam = comp.layers.addCamera({_jsx_string(name)}, [comp.width / 2, comp.height / 2]);
            {pos_call}
            return cam.name;
        }})();
    """)
    return f"Камера «{result}» добавлена в «{comp_name}»."


async def _add_path_shape_layer(
    comp_name: str, name: str, points: list[list[float]], color_hex: str, closed: bool,
    x: float, y: float,
) -> str:
    if len(points) < 2:
        raise ValueError("Нужно минимум 2 точки для path-фигуры.")
    for i, p in enumerate(points):
        if not isinstance(p, list) or len(p) != 2:
            raise ValueError(f"points[{i}] должен быть парой [x, y], получено: {p!r}.")
    r, g, b = _parse_color_hex(color_hex)
    vertices_js = "[" + ", ".join(_js_value(p) for p in points) + "]"
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layers.addShape();
            layer.name = {_jsx_string(name)};
            var contents = layer.property("ADBE Root Vectors Group");
            var group = contents.addProperty("ADBE Vector Group");
            var groupContents = group.property("ADBE Vectors Group");
            var shapeGroup = groupContents.addProperty("ADBE Vector Shape - Group");
            var shapeProp = shapeGroup.property("ADBE Vector Shape");
            var path = new Shape();
            path.vertices = {vertices_js};
            path.closed = {str(closed).lower()};
            shapeProp.setValue(path);
            var fill = groupContents.addProperty("ADBE Vector Graphic - Fill");
            fill.property("ADBE Vector Fill Color").setValue([{r}, {g}, {b}]);
            layer.property("Position").setValue([{float(x)}, {float(y)}]);
            return layer.name;
        }})();
    """)
    return f"Слой-контур добавлен в «{comp_name}»: {result}"


async def _delete_layer(comp_name: str, layer_name: str) -> str:
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layer({_jsx_string(layer_name)});
            var name = layer.name;
            layer.remove();
            return name;
        }})();
    """)
    return f"Слой «{result}» удалён из «{comp_name}»."


async def _duplicate_layer(comp_name: str, layer_name: str, new_name: str) -> str:
    comp_expr = await _find_comp_snippet(comp_name)
    rename_call = f"dup.name = {_jsx_string(new_name)};" if new_name else ""
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layer({_jsx_string(layer_name)});
            var dup = layer.duplicate();
            {rename_call}
            return dup.name;
        }})();
    """)
    return f"Слой «{layer_name}» продублирован в «{comp_name}»: {result}"


async def _set_layer_enabled(comp_name: str, layer_name: str, enabled: bool) -> str:
    # Visibility ("video" eye toggle) lives on the Layer object itself, not
    # under a Property -- unlike everything animate_property/set_property_value
    # touch, so it needs its own action rather than being foldable into those.
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layer({_jsx_string(layer_name)});
            layer.enabled = {str(enabled).lower()};
            return layer.name + {_jsx_field_sep()} + layer.enabled;
        }})();
    """)
    name, state = result.split(_FIELD_SEP)
    return f"Слой «{name}»: видимость {'включена' if state == 'true' else 'выключена'}."


async def _set_layer_time(
    comp_name: str, layer_name: str,
    start_time: float | None, in_point: float | None, out_point: float | None,
) -> str:
    if start_time is None and in_point is None and out_point is None:
        raise ValueError("Нужно указать хотя бы одно из: start_time, in_point, out_point.")
    comp_expr = await _find_comp_snippet(comp_name)
    body = []
    if start_time is not None:
        body.append(f"layer.startTime = {float(start_time)};")
    if in_point is not None and out_point is not None:
        # AE rejects setting inPoint past the layer's *current* outPoint (or
        # vice versa) -- whichever of the two we set first can transiently
        # violate that against the old value even though the final pair is
        # valid, so try both orders instead of picking one that only works
        # for shrinking-or-growing in one direction.
        body.append(f"""
            try {{ layer.inPoint = {float(in_point)}; layer.outPoint = {float(out_point)}; }}
            catch (e) {{ layer.outPoint = {float(out_point)}; layer.inPoint = {float(in_point)}; }}
        """)
    elif in_point is not None:
        body.append(f"layer.inPoint = {float(in_point)};")
    elif out_point is not None:
        body.append(f"layer.outPoint = {float(out_point)};")
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layer({_jsx_string(layer_name)});
            {"".join(body)}
            return layer.name + {_jsx_field_sep()} + layer.inPoint + {_jsx_field_sep()} + layer.outPoint;
        }})();
    """)
    name, in_pt, out_pt = result.split(_FIELD_SEP)
    return f"Слой «{name}»: inPoint={float(in_pt):.2f}с, outPoint={float(out_pt):.2f}с."


async def _list_effects(comp_name: str, layer_name: str) -> str:
    # Effect/parameter *display* names are locale-dependent (see the
    # _EFFECT_PARAM_MATCH_NAMES comment above) and _EFFECT_PARAM_MATCH_NAMES
    # only covers a handful of common effects -- this reads the real,
    # locale-independent match names straight off whatever's actually
    # applied, so add_effect's effect_params_json can target any effect
    # instead of only the ones in that table.
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layer({_jsx_string(layer_name)});
            var parade = layer.property("ADBE Effect Parade");
            var out = [];
            for (var i = 1; i <= parade.numProperties; i++) {{
                var fx = parade.property(i);
                var params = [];
                for (var j = 1; j <= fx.numProperties; j++) {{
                    var p = fx.property(j);
                    params.push(p.name + " (" + p.matchName + ")");
                }}
                out.push(fx.name + " [" + fx.matchName + "]" + {_jsx_field_sep()} + params.join(", "));
            }}
            return out.join({_jsx_record_sep()});
        }})();
    """)
    if not result:
        return f"На слое «{layer_name}» нет эффектов."
    lines = []
    for row in result.split(_RECORD_SEP):
        header, params = row.split(_FIELD_SEP, 1)
        lines.append(f"- {header}: {params or '(нет параметров)'}")
    return f"Эффекты на слое «{layer_name}» («{comp_name}»):\n" + "\n".join(lines)


async def _set_property_value(comp_name: str, layer_name: str, property_name: str, value) -> str:
    # animate_property always leaves a keyframe behind, even for a value
    # that should just be static -- this sets a property's value directly
    # with no keyframe, for callers that don't want to animate anything.
    comp_expr = await _find_comp_snippet(comp_name)
    result = await _eval(f"""
        (function() {{
            var comp = {comp_expr};
            var layer = comp.layer({_jsx_string(layer_name)});
            var prop = layer.property({_jsx_string(property_name)});
            prop.setValue({_js_value(value)});
            return layer.name + {_jsx_field_sep()} + prop.name;
        }})();
    """)
    layer, prop_name = result.split(_FIELD_SEP, 1)
    return f"«{prop_name}» на слое «{layer}» установлено в {value!r}."


AfterEffectsAction = Literal[
    "info", "list_compositions", "list_layers", "open_project",
    "create_composition", "add_text_layer", "add_solid_layer",
    "add_shape_layer", "add_path_shape_layer", "add_footage_layer",
    "animate_property", "set_property_value", "add_effect", "list_effects",
    "set_expression", "precompose", "set_3d", "add_camera",
    "delete_layer", "duplicate_layer", "set_layer_enabled", "set_layer_time",
    "add_to_render_queue", "render", "save", "save_as",
]

AEAnimatableProperty = Literal["Position", "Opacity", "Scale", "Rotation"]
AEShapeType = Literal["rectangle", "ellipse"]


@register_impl("after_effects_control")
@log_call("after_effects_control")
async def _after_effects_control(
    *,
    action: str,
    comp_name: str = "",
    text: str = "",
    font_size: int = 72,
    layer_name: str = "",
    color: str = "FFFFFF",
    width: int = 1920,
    height: int = 1080,
    duration: float = 5.0,
    frame_rate: float = 30.0,
    output_path: str = "",
    project_path: str = "",
    property_name: str = "",
    keyframes_json: str = "",
    shape_type: str = "rectangle",
    shape_width: float = 100.0,
    shape_height: float = 100.0,
    pos_x: float = 0.0,
    pos_y: float = 0.0,
    footage_path: str = "",
    effect_name: str = "",
    effect_params_json: str = "",
    expression: str = "",
    layer_names_json: str = "",
    new_comp_name: str = "",
    move_all_attributes: bool = True,
    enabled: bool = True,
    path_points_json: str = "",
    closed: bool = True,
    camera_position_json: str = "",
    discard_unsaved: bool = False,
    new_layer_name: str = "",
    value_json: str = "",
    start_time: float | None = None,
    in_point: float | None = None,
    out_point: float | None = None,
) -> dict:
    if config.SYSTEM != "Windows":
        return {"status": "error", "message": "Автоматизация After Effects доступна только на Windows."}

    try:
        if action == "info":
            message = await _get_info()
        elif action == "list_compositions":
            message = await _list_compositions()
        elif action == "list_layers":
            if not comp_name:
                return {"status": "error", "message": "Не указано имя композиции."}
            message = await _list_layers(comp_name)
        elif action == "open_project":
            if not project_path:
                return {"status": "error", "message": "Не указан project_path."}
            message = await _open_project(project_path, discard_unsaved)
        elif action == "create_composition":
            if not comp_name:
                return {"status": "error", "message": "Не указано имя композиции."}
            message = await _create_composition(comp_name, width, height, duration, frame_rate)
        elif action == "add_text_layer":
            if not comp_name or not text:
                return {"status": "error", "message": "Нужны comp_name и text."}
            message = await _add_text_layer(comp_name, text, font_size)
        elif action == "add_solid_layer":
            if not comp_name:
                return {"status": "error", "message": "Не указано имя композиции."}
            message = await _add_solid_layer(comp_name, layer_name or "Solid", color)
        elif action == "add_shape_layer":
            if not comp_name:
                return {"status": "error", "message": "Не указано имя композиции."}
            message = await _add_shape_layer(
                comp_name, shape_type, layer_name or shape_type.capitalize(),
                shape_width, shape_height, color, pos_x, pos_y,
            )
        elif action == "add_path_shape_layer":
            if not comp_name or not path_points_json:
                return {"status": "error", "message": "Нужны comp_name и path_points_json."}
            try:
                points = json.loads(path_points_json)
            except Exception as exc:
                return {"status": "error", "message": f"path_points_json не разобрался как JSON: {exc}"}
            message = await _add_path_shape_layer(
                comp_name, layer_name or "Path", points, color, closed, pos_x, pos_y,
            )
        elif action == "add_footage_layer":
            if not comp_name or not footage_path:
                return {"status": "error", "message": "Нужны comp_name и footage_path."}
            message = await _add_footage_layer(comp_name, footage_path)
        elif action == "precompose":
            if not comp_name or not layer_names_json or not new_comp_name:
                return {"status": "error", "message": "Нужны comp_name, layer_names_json и new_comp_name."}
            try:
                layer_names = json.loads(layer_names_json)
            except Exception as exc:
                return {"status": "error", "message": f"layer_names_json не разобрался как JSON: {exc}"}
            message = await _precompose(comp_name, layer_names, new_comp_name, move_all_attributes)
        elif action == "set_3d":
            if not comp_name or not layer_name:
                return {"status": "error", "message": "Нужны comp_name и layer_name."}
            message = await _set_3d(comp_name, layer_name, enabled)
        elif action == "add_camera":
            if not comp_name:
                return {"status": "error", "message": "Не указано имя композиции."}
            position = None
            if camera_position_json:
                try:
                    position = json.loads(camera_position_json)
                except Exception as exc:
                    return {"status": "error", "message": f"camera_position_json не разобрался как JSON: {exc}"}
            message = await _add_camera(comp_name, layer_name or "Camera", position)
        elif action == "add_effect":
            if not comp_name or not layer_name or not effect_name:
                return {"status": "error", "message": "Нужны comp_name, layer_name и effect_name."}
            params = {}
            if effect_params_json:
                try:
                    params = json.loads(effect_params_json)
                except Exception as exc:
                    return {"status": "error", "message": f"effect_params_json не разобрался как JSON: {exc}"}
            message = await _add_effect(comp_name, layer_name, effect_name, params)
        elif action == "set_expression":
            if not comp_name or not layer_name or not property_name or not expression:
                return {"status": "error", "message": "Нужны comp_name, layer_name, property_name и expression."}
            message = await _set_expression(comp_name, layer_name, property_name, expression)
        elif action == "animate_property":
            if not comp_name or not layer_name or not property_name or not keyframes_json:
                return {
                    "status": "error",
                    "message": "Нужны comp_name, layer_name, property_name и keyframes_json.",
                }
            try:
                keyframes = json.loads(keyframes_json)
            except Exception as exc:
                return {"status": "error", "message": f"keyframes_json не разобрался как JSON: {exc}"}
            message = await _animate_property(comp_name, layer_name, property_name, keyframes)
        elif action == "delete_layer":
            if not comp_name or not layer_name:
                return {"status": "error", "message": "Нужны comp_name и layer_name."}
            message = await _delete_layer(comp_name, layer_name)
        elif action == "duplicate_layer":
            if not comp_name or not layer_name:
                return {"status": "error", "message": "Нужны comp_name и layer_name."}
            message = await _duplicate_layer(comp_name, layer_name, new_layer_name)
        elif action == "set_layer_enabled":
            if not comp_name or not layer_name:
                return {"status": "error", "message": "Нужны comp_name и layer_name."}
            message = await _set_layer_enabled(comp_name, layer_name, enabled)
        elif action == "set_layer_time":
            if not comp_name or not layer_name:
                return {"status": "error", "message": "Нужны comp_name и layer_name."}
            if start_time is None and in_point is None and out_point is None:
                return {
                    "status": "error",
                    "message": "Нужно указать хотя бы одно из: start_time, in_point, out_point.",
                }
            message = await _set_layer_time(comp_name, layer_name, start_time, in_point, out_point)
        elif action == "list_effects":
            if not comp_name or not layer_name:
                return {"status": "error", "message": "Нужны comp_name и layer_name."}
            message = await _list_effects(comp_name, layer_name)
        elif action == "set_property_value":
            if not comp_name or not layer_name or not property_name or not value_json:
                return {
                    "status": "error",
                    "message": "Нужны comp_name, layer_name, property_name и value_json.",
                }
            try:
                value = json.loads(value_json)
            except Exception as exc:
                return {"status": "error", "message": f"value_json не разобрался как JSON: {exc}"}
            message = await _set_property_value(comp_name, layer_name, property_name, value)
        elif action == "add_to_render_queue":
            if not comp_name or not output_path:
                return {"status": "error", "message": "Нужны comp_name и output_path."}
            message = await _add_to_render_queue(comp_name, output_path)
        elif action == "render":
            message = await _render()
        elif action == "save":
            message = await _save_project()
        elif action == "save_as":
            if not project_path:
                return {"status": "error", "message": "Не указан project_path."}
            message = await _save_project_as(project_path)
        else:
            return {"status": "error", "message": f"Неизвестное действие after_effects_control: «{action}»."}
        return {"status": "ok", "message": message}
    except client.BridgeConnectionError as exc:
        return {
            "status": "error",
            "message": (
                f"Не удалось связаться с After Effects («{action}»): {exc}. After Effects "
                "запущен, а панель Jarvis Bridge открыта хотя бы раз (Окно > Расширения > "
                "Jarvis Bridge)?"
            ),
        }
    except Exception as exc:
        # A plain script/validation error (bad comp/layer name, malformed
        # JSON, invalid color, AE rejecting an expression, ...) already has
        # a clear message of its own -- tacking on the "is AE running?" hint
        # here (as this used to, for every exception) was actively
        # misleading for those cases, so that hint is now reserved for
        # BridgeConnectionError above.
        return {
            "status": "error",
            "message": f"Ошибка After Effects («{action}»): {exc}",
        }


@register_tool
@function_tool
async def after_effects_control(
    context: RunContext,
    action: AfterEffectsAction,
    comp_name: str = "",
    text: str = "",
    font_size: int = 72,
    layer_name: str = "",
    color: str = "FFFFFF",
    width: int = 1920,
    height: int = 1080,
    duration: float = 5.0,
    frame_rate: float = 30.0,
    output_path: str = "",
    project_path: str = "",
    property_name: str = "",
    keyframes_json: str = "",
    shape_type: AEShapeType = "rectangle",
    shape_width: float = 100.0,
    shape_height: float = 100.0,
    pos_x: float = 0.0,
    pos_y: float = 0.0,
    footage_path: str = "",
    effect_name: str = "",
    effect_params_json: str = "",
    expression: str = "",
    layer_names_json: str = "",
    new_comp_name: str = "",
    move_all_attributes: bool = True,
    enabled: bool = True,
    path_points_json: str = "",
    closed: bool = True,
    camera_position_json: str = "",
    discard_unsaved: bool = False,
    new_layer_name: str = "",
    value_json: str = "",
    start_time: float | None = None,
    in_point: float | None = None,
    out_point: float | None = None,
) -> str:
    """Control Adobe After Effects through its scripting API instead of
    clicking through the UI. Requires After Effects running with the Jarvis
    Bridge panel loaded at least once (Window > Extensions > Jarvis Bridge).

    Unfamiliar project (not one you created this session)? Call
    "list_compositions" then "list_layers" first -- every action below needs
    an exact name, not a guess.

    Args (grouped by the action(s) each one belongs to):
        action: info | list_compositions | list_layers(comp_name) |
            open_project(project_path, discard_unsaved) -- refuses on unsaved
            changes unless discard_unsaved=true |
            create_composition(comp_name,width,height,duration,frame_rate) |
            add_text_layer(comp_name,text,font_size) |
            add_solid_layer(comp_name,layer_name,color) |
            add_shape_layer(comp_name,shape_type,layer_name,shape_width,
            shape_height,color,pos_x,pos_y) |
            add_path_shape_layer(comp_name,layer_name,path_points_json,color,
            closed,pos_x,pos_y) -- arbitrary polygon outline |
            add_footage_layer(comp_name,footage_path) -- import + add a layer |
            animate_property(comp_name,layer_name,property_name,keyframes_json) |
            set_property_value(comp_name,layer_name,property_name,value_json)
            -- static value, no keyframe |
            add_effect(comp_name,layer_name,effect_name,effect_params_json) |
            list_effects(comp_name,layer_name) -- see existing effects +
            match names; use before add_effect if effect_name isn't one of
            Gaussian Blur/Glow/Drop Shadow/Hue-Saturation/Curves/Fill/Tint/
            Basic 3D, or a param name isn't recognized |
            set_expression(comp_name,layer_name,property_name,expression) --
            runs in AE's sandboxed expression engine (no file/network/process
            access), safe for open-ended text unlike running a script |
            precompose(comp_name,layer_names_json,new_comp_name,
            move_all_attributes) |
            set_3d(comp_name,layer_name,enabled) |
            add_camera(comp_name,layer_name,camera_position_json) |
            delete_layer(comp_name,layer_name) |
            duplicate_layer(comp_name,layer_name,new_layer_name) |
            set_layer_enabled(comp_name,layer_name,enabled) -- show/hide |
            set_layer_time(comp_name,layer_name,start_time,in_point,out_point)
            -- at least one of the three |
            add_to_render_queue(comp_name,output_path) | render | save |
            save_as(project_path).
        comp_name, layer_name: Target composition/layer. layer_name also
            doubles as the new layer's name for add_solid_layer/
            add_shape_layer/add_path_shape_layer/add_camera.
        text, font_size: Content/size for add_text_layer.
        color: Hex like "FF0000", for add_solid_layer/add_shape_layer/
            add_path_shape_layer.
        width, height, duration, frame_rate: For create_composition.
        output_path, project_path: File paths, for add_to_render_queue and
            save_as/open_project respectively.
        property_name: "Position"/"Scale" take [x, y]; "Opacity" (0-100) and
            "Rotation" (degrees) take a number; for set_expression, any
            property or effect-parameter name.
        keyframes_json: JSON array for animate_property, each {"time":
            <seconds>, "value": <number or [x,y]>, "ease": "linear"|"ease"|
            "ease_in"|"ease_out"}, e.g. '[{"time":0,"value":[100,400]},
            {"time":2,"value":[900,400],"ease":"ease"}]'.
        value_json: Static JSON value for set_property_value, e.g. '[960,540]'.
        shape_type, shape_width, shape_height, pos_x, pos_y: Shape geometry/
            position, for add_shape_layer/add_path_shape_layer.
        footage_path: Local image/video path, for add_footage_layer.
        effect_name: Display name (e.g. "Gaussian Blur", "Glow", "Drop
            Shadow"), for add_effect.
        effect_params_json: JSON object of param->value, e.g.
            '{"Blurriness": 40}', for add_effect.
        expression: ExtendScript expression source, e.g. 'wiggle(2, 30)'.
        layer_names_json, new_comp_name, move_all_attributes: For precompose
            -- which layers, the new comp's name, whether their existing
            animation/effects move with them (true = usual choice).
        enabled: 3D on/off for set_3d, or show/hide for set_layer_enabled.
        new_layer_name: Optional name for duplicate_layer's copy.
        start_time, in_point, out_point: Seconds on the timeline, for
            set_layer_time (start_time shifts the whole layer).
        path_points_json: [x,y] points in the layer's local space, e.g.
            [[0,0],[100,0],[50,-80]] for a triangle.
        closed: Whether the path outline closes, for add_path_shape_layer.
        camera_position_json: Optional [x,y,z] for add_camera.
        discard_unsaved: Irreversible -- only set true for open_project after
            the user has clearly said to discard unsaved work.
    """
    result = await _after_effects_control(
        action=action, comp_name=comp_name, text=text, font_size=font_size,
        layer_name=layer_name, color=color, width=width, height=height,
        duration=duration, frame_rate=frame_rate, output_path=output_path,
        project_path=project_path, property_name=property_name,
        keyframes_json=keyframes_json, shape_type=shape_type,
        shape_width=shape_width, shape_height=shape_height, pos_x=pos_x,
        pos_y=pos_y, footage_path=footage_path, effect_name=effect_name,
        effect_params_json=effect_params_json, expression=expression,
        layer_names_json=layer_names_json, new_comp_name=new_comp_name,
        move_all_attributes=move_all_attributes, enabled=enabled,
        path_points_json=path_points_json, closed=closed,
        camera_position_json=camera_position_json, discard_unsaved=discard_unsaved,
        new_layer_name=new_layer_name, value_json=value_json,
        start_time=start_time, in_point=in_point, out_point=out_point,
    )
    return result["message"]
