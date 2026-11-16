# Curriculum catalog

`catalog.json` connects the active creative app to its courses, modules, and lesson files.
The app ID comes from `data/contexts.json` (loaded by `UIGraph`), so Course mode only offers
curriculum for the software Dydact is currently configured to guide.

```text
curriculum/
  catalog.json
  <app-id>/
    <lesson paths listed by that app's modules>
```

Each app entry has a display `title` and a `courses` object. Each course has a display `title`
and an ordered `modules` list. A module has a unique `id`, display `title`, and a `lesson` path
relative to that app's folder. Lesson paths are checked to stay inside that app's curriculum
folder. If a lesson JSON includes an `app` field, it must match the active app ID.

To add another creative app, add its ID to `catalog.json` and provide matching app-specific
contexts in `data/contexts.json`; then add courses and modules that point to its lesson JSON
files. Adding a catalog entry alone does not switch Dydact's screen capture or icon references
to another app.
