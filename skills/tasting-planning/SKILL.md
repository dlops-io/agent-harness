---
name: tasting-planning
description: Plan and present a cheese tasting using a confirmed menu, serving guidance, and a reusable tasting-plan format. Use for tasting or event planning, not simple stock lookups.
---

# Tasting planning

Read `references/serving-guide.md` and `assets/tasting-plan.md` when preparing a
tasting plan. Use the format to make serving order, quantities, pairings and open
questions easy for the host to review.

1. Keep a short task list for a multi-step event.
2. Call `assess_event` for the confirmed menu. Use its exact quote and violations;
   do not invent quantities, prices or available inventory.
3. Use the serving guide to arrange a gentle-to-intense tasting sequence. Explain
   any departure from that sequence. Use only tool-returned compatible pairings.
4. Preserve the customer's hard constraints. Policy numbers come from the current
   shop configuration and validation tools, never from this skill.
5. If there is a shortfall and the task includes sourcing, consider the
   `vendor-outreach` skill. A conditional sourcing menu is not an accepted order.
6. Use the asset to organize the plan. When returning `TastingReply`, the application
   supplies its headings: put serving order and pairing IDs in `courses`, practical
   serving advice only in `serving_notes`, and unresolved availability or decisions
   in `open_questions`. Do not paste the full template into any field. Complete only
   the work actually performed.

This skill guides planning. It does not place an order, change customer details,
grant approval or override the checkout and email policies.
