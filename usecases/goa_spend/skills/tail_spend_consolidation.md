# Skill: tail spend & consolidation
Mirrors the native `tail_spend_consolidation` skill. Use for tail spend, vendor consolidation,
fragmentation, small-value transactions, single-source risk, or catalog-implementation candidates.

- Definitions: **tail spend** = vendors below a threshold (common cuts: <$10K, <$25K, <$50K -- ask if
  unspecified); **fragmentation** = a category with many vendors and no dominant one (>5 vendors and top-2
  share < 50%); **single-source risk** = one vendor > 80% of a category's spend; **consolidation** =
  multiple vendors in the same category that could be rationalized.
- Roll vendors up by **ParentVendorName**; scope to categorized spend (`ExecutiveCategory IS NOT NULL`).
- "Suitable for catalog" is a judgment call -- flag standardizable goods/services categories rather than
  asserting it. Tail vendor counts inflate when ParentVendorName normalization is incomplete.
