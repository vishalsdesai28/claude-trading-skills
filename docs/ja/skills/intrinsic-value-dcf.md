---
layout: default
title: "Intrinsic Value Dcf"
grand_parent: 日本語
parent: スキルガイド
nav_order: 35
lang_peer: /en/skills/intrinsic-value-dcf/
permalink: /ja/skills/intrinsic-value-dcf/
generated: true
---

# Intrinsic Value Dcf
{: .no_toc }

Produce a triangulated intrinsic-value estimate for a US ticker by blending a discounted-cash-flow (DCF) model, peer-median relative multiples, and an optional sum-of-the-parts. Use when the user asks "what is X worth", "fair value of X", "intrinsic value", "build a DCF", "DCF for X", "WACC", "terminal value", "implied share price", "upside to fair value", "is X overvalued/undervalued", "peer/relative valuation", "EV/EBITDA target", or "sum of the parts". Builds WACC from CAPM with a live 10Y UST risk-free rate, projects 5-year unlevered FCFF with growth fade, computes dual terminal value, emits a fully-recalculated WACC x terminal-growth sensitivity grid plus Bull/Base/Bear scenarios, applies guardrail gates, and routes by sector (banks -> P/TBV, REITs -> P/FFO, SaaS -> EV/Revenue + Rule of 40).
{: .fs-6 .fw-300 }

<span class="badge badge-api">FMP必須</span>

[GitHubでソースを見る](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/intrinsic-value-dcf){: .btn .fs-5 .mb-4 .mb-md-0 }

> **Note:** This page has not yet been translated into Japanese.
> Please refer to the [English version]({{ '/en/skills/intrinsic-value-dcf/' | relative_url }}) for the full guide.
{: .warning }

---

[English版ガイドを見る]({{ '/en/skills/intrinsic-value-dcf/' | relative_url }}){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
