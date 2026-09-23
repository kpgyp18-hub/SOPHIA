---
name: authorized-security-validation
description: Plan or review authorized security validation with explicit scope, non-destructive checks, evidence, and remediation; not for exploitation or unauthorized testing.
---

# Authorized Security Validation

Validate defensive posture only on systems the user owns or is explicitly authorized to assess. Keep the work bounded, reversible, and evidence-led.

## Before testing

1. Record the asset owner, written authorization, in-scope targets, allowed methods, time window, rate/noise limits, and an emergency stop contact. If any of these are missing, limit work to passive review or ask for direction.
2. Prefer configuration review, dependency/secret scanning, log inspection, and safe dry runs before active probes. Do not use production credentials, exploit payloads, persistence, evasion, credential access, lateral movement, or exfiltration unless the user separately authorizes them.
3. Establish a rollback or containment plan for any action that can change data, availability, or alerts.

## Execute and report

1. Test the smallest representative scope first. Capture exact commands, timestamps, targets, tool versions, and raw evidence; stop on unexpected impact or a scope boundary.
2. Separate observed facts from inferred attack paths. Reproduce a finding safely before assigning severity or claiming exploitability.
3. Deliver affected asset, evidence, impact, remediation, verification method, and residual risk. Redact secrets and personal data from all outputs.

## References

- Community cybersecurity-skill catalog and framework mapping: <https://github.com/mukul975/Anthropic-Cybersecurity-Skills>
- NIST Cybersecurity Framework 2.0: <https://www.nist.gov/cyberframework>
