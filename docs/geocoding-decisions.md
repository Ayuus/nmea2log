# Port name decisions (`geocode.py`)

This document records **which choices** were made in how `geocode.py` turns a GPS position into a
port name, and above all **why** — including what was tried and didn't work. Meant to keep a
future change from walking down the same dead ends again.

All claims below are verified against real Nominatim/Overpass data for the 15 real trip stops
from a real logbook (see "Reference set" at the bottom) — not against assumptions.

## Current rules (as of commit `1b567e9`)

1. **An islet found via Overpass always wins**, regardless of distance. Only a plain name
   **node** counts; a coastline **way** is ignored, even as a fallback.
2. **Nominatim's own marina/leisure match wins**, unless that match is a bare point **node**
   (no polygon/way) *and* the address also contains a real village/town/city — then the village
   wins.
3. Otherwise: Nominatim's own `_PREFERRED_ADDRESS_KEYS` order (leisure/marina/harbour before
   town/village/city/...), unchanged since the start of the project.
4. A match more than 250 m away gets a prefix ("alongside, near" / "on the water, near") instead
   of implying the boat is right there.

## Decision 1: islet — node always wins, way is ignored

**What**: `_nearby_islet_name` now only asks Overpass for `node["place"="islet"]`. The `way`
variant (coastline) is no longer queried, and where it once was, is never used — not even when no
node is found.

**Why**:
- An islet is usually mapped twice in OSM: a plain name node (e.g. "Île de la Jument") and a
  separate coastline way that can carry a more elaborate name (e.g. "Île de la Jument (Er
  Gazeg)"). The node's name is shorter and more recognizable for a boat simply anchored off it,
  not literally in that one cove — explicitly requested this way ("always use the node").
- A **way**'s reported distance is unreliable: Overpass' `out center` returns the geometric
  **centroid** of the whole shape, not the nearest point of the coastline. For a large/elongated
  island, that centroid can be far from the boat while an edge of the coastline still falls within
  the search radius (300 m) — so Overpass returns the way anyway. **Real case**: at Loctudy the
  boat was 38 m from the harbour pier (clearly there), but Île Garo's way centroid computed to
  ~680 m and would have wrongly outvoted the harbour if the way had been used as a fallback.
- A node has one exact point, so this problem doesn't apply to it.

**What didn't work / was rejected**:
- *Way as a fallback with a distance cutoff*: seemed like an option, but Overpass' own
  `around:300` filter already guarantees the way's geometry itself is within 300 m — the problem
  is the centroid point, not the real distance. A distance cutoff on the centroid would not have
  reliably stopped Île Garo.
- *Islet only wins if it's closer than the village*: explicitly rejected at Kerners — "islet only
  winning when closer doesn't work in this case either" / "always use the node". The whole point
  of anchoring near an islet is often the islet itself, even if the village happens to be slightly
  closer.

## Three regressions in the live rollout (found during a real production run)

The Overpass query simplification from Decision 1 above introduced, alongside the intended
change, three separate bugs — only visible during a real run against thousands of `.ebl` files,
not in the tests (which use mocked responses) and not in the pre-rollout verification either
(which reused a locally cached Overpass answer, built from a separate, independently written
query function — not `geocode.py`'s own).

1. **Missing `[out:json]`** (commit `3c0b757`): without that instruction Overpass answers with
   its own default format (XML, HTTP 200) instead of an error — every call therefore failed
   guaranteed on `json.loads`, not just occasionally under load. Visible in the log file as
   `Expecting value: line 1 column 1 (char 0)`, over and over.
2. **A server-side timeout that looks like "nothing found"** (commit `bb2c241`): Overpass also
   answers with HTTP 200 and valid JSON when the query itself was aborted server-side by a time
   limit — with a `"remark"` field and an empty/incomplete `"elements"` list. That wasn't
   recognized before, and so got permanently cached as a confirmed "no islet here".
3. **`out tags;` returns no coordinates** (commit `1b567e9`): without the `center` modifier,
   Overpass returns only `id` + `tags` for a node, no `lat`/`lon` — so the node *was* found, but
   without a position to compute a distance from, and silently dropped out as if nothing had been
   found. This result, too, was (wrongly) cached permanently.

**Lesson for next time**: all three are exactly the kind of bug that a mocked test or a reused
local cache file won't catch, because both assume the query construction itself is already
correct. The only way this came to light was a **real, full run against the live Overpass
service** using the actual query function from `geocode.py` itself — not a standalone test script
with its own, separately written query. For a future change to the Overpass query: run at least
one standalone `_nearby_islet_name(...)` call directly against the live service (as under
"Reference set" above), not just `pytest` and not just a simulation on previously cached data.

Separate, unrelated issue during the same session: an old, never-explained upload of test-fixture
data (1 trip, round coordinates from `tests/test_trip_ids.py`) was live for a while — not caused
by any code in this project, and disappeared on its own once a real run uploaded again. Root
cause never found; no action taken beyond reconfirming that none of the fixes or test runs above
could have caused it (every upload test explicitly isolates itself from the real `nmea2log.ini`,
see `tests/test_cli.py`).

## Decision 2: marina node vs. village

**What**: `_pick_place_name` uses Nominatim's own `leisure` match (usually a marina) as always —
**except** when that match is a bare point node (`osm_type == "node"`, no polygon) *and* the
address also contains a real village/town/municipality. Then the village wins.

**Why**:
- **Real case**: at 47.4889,-3.1012 (Quiberon), Nominatim matches an OSM node called "Darse de
  Castéro" (a small, specifically named corner) instead of the well-known, recognizable "Port
  Haliguen" — which *was* present in the same address, but never used because the `leisure`
  shortcut in `_pick_place_name` always checks first.
- **Distinguishing feature found**: how the marina itself is mapped in OSM.
  - Port Olona, Port de Plaisance de Pornichet, Port du Crouesty, Concarneau: all `osm_type:
    way` — a real drawn harbour basin (bounding box hundreds of metres across).
  - Darse de Castéro: `osm_type: node` — a bare point (bounding box ~11×11 m, no real polygon).
  - This is a **stable, tag-based** property (how the element is mapped), not something that can
    vary between Nominatim calls.

**What didn't work / was rejected** (each tested against all 15 real positions before being
rejected):

| Attempt | Result | Why rejected |
|---|---|---|
| Reorder `_PREFERRED_ADDRESS_KEYS` (village always before leisure/marina/harbour) | Fixes Darse de Castéro, but also changes Port Olona → "alongside, near Les Sables-d'Olonne", Pornichet → "Pornichet" (loses the marina name) | Explicitly rejected: "alongside near Port Olona is fine, please don't change it" |
| General rule: skip the `leisure` shortcut whenever a village/town is in the address | Same problem as above, plus Crouesty gave two different answers on two separate live calls ("Kerners" vs. "Port Navalo") — Nominatim's public service turned out to be inconsistent between calls here | Unpredictable, on top of also affecting Port Olona/Pornichet |
| Prefer the `marina`/`harbour` key over `leisure` (within `_PREFERRED_ADDRESS_KEYS`) | No effect whatsoever | For all 15 positions a marina match only ever comes back via the `leisure` field; `marina`/`harbour` never occur separately in this dataset in practice |
| Separate Overpass search for nearby marinas (with a `has_village` exception, once built in commit `01f5758`) | Worked, but mostly duplicated what Nominatim's own address already did | Fully removed this session: a marina genuinely missing from Nominatim's address is an OSM data gap, to be fixed via an OSM edit, not extra code (see the Piriac precedent) |
| One hardcoded exception for exactly this coordinate | Would work, affects nothing else | Rejected in favour of the node/way rule: that one is generic and covers a whole class of problems, not just this one point |

## Why not just fix it via OpenStreetMap?

For a **missing** marina (like the historical Piriac case), the agreement is: fix that via an OSM
edit, not code — see the policy decision above regarding the Overpass marina search.

For Darse de Castéro it was different: the data itself isn't wrong (it's a legitimate, correctly
tagged marina node), it's just **less relevant** than the village next to it for someone reading
the logbook. That's not a data error to fix in OSM, but a presentation choice — hence the code
fix.

## Reference set: the 15 real trip stops

Used to check every proposed rule against before it was built. For a future change: regenerate
this table first and compare before changing anything.

| Coordinate | Result (as of `6aa7f02`) |
|---|---|
| 46.4968,-1.7899 | Les Sables-d'Olonne |
| 46.5005,-1.7950 | alongside, near Port Olona |
| 47.1080,-2.1157 | Pornic |
| 47.2579,-2.3507 | Port de Plaisance de Pornichet |
| 47.2749,-2.4246 | La Baule-Escoublac |
| 47.3445,-2.5135 | La Turballe |
| 47.3828,-2.5447 | Piriac-sur-Mer |
| 47.4889,-3.1012 | Port Haliguen |
| 47.5446,-2.8942 | Port du Crouesty |
| 47.5705,-2.8852 | Île de la Jument |
| 47.7108,-3.3550 | Port-Louis |
| 47.7108,-3.3551 | Port-Louis |
| 47.8387,-4.1759 | Loctudy |
| 47.8704,-3.9147 | Concarneau |
| 47.8776,-4.1213 | Sainte-Marine |

**Practical tip for a future session**: Overpass rate-limits heavily under repeated testing (often
`429`/`504`). The fastest way to iterate is to write the raw Nominatim and Overpass answers for
these 15 points to disk once (JSON, both full payloads per coordinate), then simulate every
variant of the logic directly against that local file — no further network calls needed until a
genuinely new position is added.

## Still open (deliberately deferred)

- **Recognizing locks/bridges** ("The lock at ...", "The Ketelbrug") — no real data available yet;
  an example is expected at Barrage d'Arzal. Waiting for real coordinates before this gets built.
- Whether the "islet always node, way ignored" principle should also apply to other feature types
  besides islets hasn't been investigated yet — deliberately kept scoped to islets for now, where
  the concrete problem actually occurred.
