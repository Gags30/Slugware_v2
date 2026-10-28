# Fonts

Inter 4.1 (https://github.com/rsms/inter), Light, Regular, Medium and SemiBold, under the
SIL Open Font License (OFL.txt).

These copies make Inter's tabular figures the default digits, so countdowns don't shift
sideways as they tick (Qt 5 can't turn on the `tnum` feature itself). Made from the release's
`extras/ttf` files with fontTools, by pointing each digit's cmap entry at its `tnum` glyph:

```python
from fontTools.ttLib import TTFont
f = TTFont("Inter-Regular.ttf")
gsub = f["GSUB"].table
lookups = {i for r in gsub.FeatureList.FeatureRecord if r.FeatureTag == "tnum" for i in r.Feature.LookupListIndex}
tnum = {}
for i in lookups:
    for st in gsub.LookupList.Lookup[i].SubTable:
        tnum.update(getattr(st.ExtSubTable if st.LookupType == 7 else st, "mapping", {}))
for table in f["cmap"].tables:
    for cp, glyph in list(table.cmap.items()):
        if glyph in tnum:
            table.cmap[cp] = tnum[glyph]
f.save("Inter-Regular.ttf")
```
