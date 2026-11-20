# Business Rules — Alpha Management (`alpha`)

## BR-1: Saving an alpha requires the save privilege
```
IF alpha.id IS NULL THEN require(ADD_ALPHA)
```
- Source: `AlphaService.saveAlpha`.
- Note line that is not a citation.

## BR-2: An alpha in use cannot be purged
- Source: `x.AlphaService.purgeAlpha`.
- Message code `Alpha.purge.inUse` is raised.

## BR-3: Void and unvoid are symmetric
- Source: `AlphaService.voidAlpha` / `unvoidAlpha`.

## BR-4: Alpha carries a status
- Source: `Alpha.status`.

## BR-5: Alpha mode is configuration driven
The mode flag decides the path. No source line here.

## BR-6: Saving cascades to beta
- Source: `AlphaService.saveAlpha`,
  `BetaService.saveBeta`.

## BR-7: Relocated behaviour
- Source: `AlphaService.relocatedMethod`.

## BR-8: Mapping files
- Source: `alpha.hbm.xml` and `reference-documentation/coupling-hotspots.md`.
