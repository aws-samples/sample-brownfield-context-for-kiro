# Business Rules — Beta Processing (`beta`)

## BR-1: Saving a beta validates the limit
- Source: `BetaService.saveBeta`.

## BR-2 — Orders are placed through the order service
- Source: `app/services/orders.py`.

## BR-3: Alpha services coordinate betas
- Source: `AlphaServices`.

## BR-4: The zeta engine ignites betas
- Source: `ZetaEngine.ignite`.

## BR-5: Free text citation
- Source: `IF a / b THEN c`.

## BR-6: Guarded records are saved without an authorization check
- Source: `GuardedApiImpl.saveGuarded`.
- The save method has no @PreAuthorize annotation, so every caller can save a guarded record.

