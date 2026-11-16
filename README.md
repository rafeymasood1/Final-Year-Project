# Dydact

Software that teaches Adobe Illustrator from 0 to hero by guiding the learner **on their own screen**.
Today: the "show me where it is" engine works (Query mode). Next: the course layer on top (Course mode).

## Run
```
pip install -r requirements.txt
python main.py
```
Windows only (UI Automation, SetWindowPos, display affinity).

## Map: where does what live?
| Folder / file | What it is | Add here when you want to... |
|---|---|---|
| `main.py` | Builds the Qt app, wires the pieces | rarely - only new top-level windows |
| `data/contexts.json` | Everything Dydact *knows* about Illustrator's UI (tool groups, panels, menus) | add a tool / panel / panel button |
| `data/refs/<icon>/<theme>/{unselected,selected}.png` | Eight SVG renders per icon, used for themed matching | use the exact icon ID from `contexts.json` for the folder name |
| `tools/menu_tree.txt` | Menu hierarchy, human-editable | add / fix a menu command, then run `build_contexts.py` |
| `tools/crawl_menus.py` | Reads the live menus or Properties panel in Illustrator | refresh the menu tree or panel inventory |
| `tools/build_contexts.py` | Imports menu trees and Properties snapshots into `contexts.json` | after editing or crawling the UI |
| `dydact/vision/` | Seeing the screen: `capture`, `find_icon` (icons), `ocr` (menu-bar text), `coords` | better matching, new monitors, DPI |
| `dydact/uigraph/graph.py` | UI graph + BFS: "what must I do to reach X?" | new kinds of "reveal" (dialog tabs, context menus) |
| `dydact/guide/guide.py` | `Guide`: runs one query, step by step, using vision + overlay | step behaviour, retry logic |
| `dydact/guide/menu_nav.py` | Highlights items inside open dropdowns via UI Automation | menu bugs |
| `dydact/guide/clicks.py` | Detects the learner's click / hold | drag / key detection |
| `dydact/overlay/overlay.py` | Transparent click-through drawing layer | **guidance animations** (ghost cursor, ripple, drag path) -> add `animations.py` |
| `dydact/ui/frontend.py` | D button, menu, Query / Settings / Course pages | build `course_page.py`, plug it in |
| `dydact/course/` | shared lesson model, runner, checks, and progress | edit reusable course behavior |
| `curriculum/<app>/<course>/<segment>/` | lesson YAML and local media; see `curriculum/README.md` | add a lesson to its app/course/segment |
| `assets/animations/` | reusable concept explainers (Lottie / video) | share media across lessons |

## Getting the full menu list without screenshots
```
python tools/crawl_menus.py                                   # Illustrator open and in front
python tools/build_contexts.py tools/menu_tree.crawled.txt
python -m dydact.uigraph.graph --check
```
`crawl_menus.py` reads the native popup hierarchy on English Illustrator for Windows,
including installed workspaces, toolbars, and libraries. It omits open and recent
document names and the installed and recent font lists.
After a live build, `menus.unverified_items` lists entries retained from earlier builds.
`--prune` removes commands the crawl did not see. Do NOT use it after a partial crawl (`--only`).
Descriptions and needs_selection flags you wrote are kept.
If the crawler finds no menu bar, try `python tools/crawl_menus.py --probe`. For Illustrator's
English shortcut export, choose **Edit > Keyboard Shortcuts > Export Text** and import it with
the existing tree:

```powershell
python tools/build_contexts.py tools/menu_tree.txt --shortcut-export tools/illustrator-shortcuts.crawled.txt
```

The export is flat. The builder updates shortcuts by top-level menu and unambiguous command
label, using the tree for submenu paths. Existing commands outside the export remain available.

## Capturing the Properties panel

In Query mode, `properties` finds the docked tab through Windows UI Automation.
If Dydact cannot locate the tab, it guides you through **Window > Properties**.

Open an Illustrator document and choose **Window > Properties**. Deselect the artwork for the first capture.

```powershell
python tools/crawl_menus.py --properties --context no_selection
python tools/build_contexts.py --properties tools/properties.crawled.json
```

Select artwork or change tools and repeat with a different `--context` name, such as `shape_selected`.
The default capture file is overwritten on each crawl. Use `--out` to keep separate snapshots.
The raw capture file contains the accessibility tree. The builder matches its controls against
`panels.properties.elements` and stores the matching query IDs in `panels.properties.contexts`.
Importing the same state replaces its list. In Query mode, enter a control ID such as `fill`,
`font_size`, or `document_units`. Dydact uses the control's live accessibility bounds within
Properties, so these targets don't require reference images.

## Menu label that flips with state
`"Hide Edges|Show Edges"` in the tree = either wording matches (Illustrator renames these commands).

## Still unverified / to do
- `menu_tree.txt` is the manually maintained fallback. Use the live crawl to refresh the installed menu catalog.
- Tool groups (`groups` in contexts.json) and `verify` notes: confirm by long-pressing tools.
- Panel buttons (Pathfinder, Stroke, Align, Swatches, Color, Gradient...) are not in contexts.json yet:
  each needs refs/ crops, then an entry under `panels`.
- Course runner, course page, checks against the real document, animations: skeletons only.


### Canvas-aware search

The Guide searches the visible configured application, not a left-hand strip.
`data/layouts/<app>.json` configures executable names, accessibility selectors,
canvas inset, refresh interval, and optional manual canvas rectangles. The app
name comes from `data/contexts.json`. Menu routing and icon reference preparation
still have Illustrator-specific assumptions; a layout profile alone is not a
complete Blender integration.

The Illustrator profile recognizes accessible Document controls or the verified
`OWL.Document` / inner `DroverLord - Window Class` viewport relationship. The
viewport must share its container origin, be smaller on both axes, and occupy at
least half its area; this avoids selecting document status controls. Bounds come
from the live windows, not fixed screen coordinates. A 12-pixel inset preserves
canvas-edge controls. Native toolbars and status inputs remain searchable.
These rules were checked against the supplied Illustrator window inventory;
other versions/workspaces may expose different controls. Blender intentionally has no guessed canvas selectors;
it searches its whole visible window until a verified profile is supplied.

Search uses rectangular crops outside excluded regions, retaining a visibility
mask to reject covered pixels. A template may extend across a crop edge but its
center must be searchable. Last-location checks use that same mask. Open flyouts
bypass canvas exclusions and search beside their opener. Theme/scale reuse remains.
Highly fragmented layouts fall back to one bounding crop with an exact mask.

Accessibility targets only the configured app's native windows, then runs UIA in a
separate process with a two-second runtime timeout, refreshed
at most every three seconds (immediately when visible window bounds change).
Layout changes within those bounds may take up to that interval to be recognized.
Failed probes discard stale exclusions and search the full visible app. This
version does not infer canvas from screenshot colors. Embedded horizontal
toolbars inside a known canvas are conservatively preserved using neutral chrome
components, bar geometry, and distributed content edges. This visual heuristic
can add extra search area for artwork resembling a toolbar; unusual palettes or
layouts may need profile adjustment. It runs on each captured frame so a moved
contextual toolbar does not wait for the accessibility cache. Detection and expanded search have overhead; speed is not yet benchmarked
on a real workspace.

With Illustrator visible, run:

```powershell
.venv\Scripts\python.exe -m dydact.vision.layout_preview --app illustrator
```

The diagnostic preview allows 15 seconds (`--timeout` overrides this); the app
keeps its short runtime timeout. Timeout errors are shown explicitly.

This saves `data/shots/layout_preview.png`, its `.json` region report, and
`.nodes.json` accessibility inventory. Red is excluded (including other apps),
green outlines preserved UI. The status explicitly says when no canvas was found.
For offline review, pass `--shot screenshot.png --nodes layout_preview.nodes.json`;
these must use the same monitor coordinates. Offline previews assume every pixel
belongs to the app, since the live visibility mask is unavailable.

Selectors are AND combinations of full-match, case-insensitive regexes on
`control_type`, `name`, `class_name`, and `automation_id`; separate selectors are
OR alternatives. Do not add broad Pane selectors without checking the preview.
`manual_canvas_regions` accepts normalized `[x,y,width,height]` boxes relative to
visible app bounds; these are explicit workspace configuration, not detection.
Multiple canvas regions are supported. Controls in `preserve_selectors` and
floating app windows override exclusions.

Run checks with `.venv\Scripts\python.exe -m unittest discover -s tests -v`.
