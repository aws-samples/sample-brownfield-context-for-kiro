# Sign-off checklist

`review render` writes `signoff-checklist-<slug>.md` per domain. Tick each line (`- [x]`) that holds, fill the fields, and pass the file to `review signoff --checklist-file`. Only these six keys are read; any other key is an error (exit 3).

```
- [ ] all_claims_have_verdict: Every BR claim in this domain has a verdict other than unreviewed.
- [ ] evidence_checked: I opened the evidence for each verified and incorrect claim and agree with it.
- [ ] incorrect_claims_have_corrections: Every incorrect or needs-edit claim states what the code actually does.
- [ ] needs_sme_items_listed: Every needs-sme claim has a question an SME can answer.
- [ ] suspected_defects_raised_with_owner: Each suspected defect is logged with an owner outside this review.
- [ ] requirements_source_consulted: Where intent was unclear, I checked the requirements or asked the product owner.
```

Then: `Reviewer:`, `Role:`, `Reviewer type (kiro|sme|kiro (AI stand-in for SME)):`, `Date:`, `Decision (approved|approved-with-exceptions|rejected):`, `Exceptions (BR ids):`.

How the sign-off changes what Kiro sees: an SME sign-off labels each rule "SME-signed by <name>"; an SME exception keeps the rule but marks it NEEDS SME (a needs-edit or incorrect rule keeps its correction); an SME rejection makes the whole domain UNREVIEWED; a Kiro sign-off is labelled "AI-reviewed, not SME-signed"; an AI stand-in is labelled "AI stand-in for SME (Kiro), not SME-signed". Only reviewer type `sme` counts as SME.
