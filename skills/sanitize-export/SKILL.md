---
name: sanitize-export
description: >
  Sanitize SkillBench export JSON by intelligently finding and redacting sensitive
  data before sharing. The AI agent scans the export, discovers user-specific
  sensitive patterns, and produces a clean version. Use when: user runs
  `skillbench gather`, wants to review/clean export data, says "sanitize", "redact",
  "clean export", "remove secrets", or "prepare for upload".
---

# Sanitize SkillBench Export

You are sanitizing a SkillBench session export (`dist/skillbench_export.json`) to
remove sensitive data before the user shares it. The export contains coding agent
conversations (prompts, responses, tool output) — these commonly leak secrets,
PII, and internal infrastructure details.

## Procedure

### 1. Sample the data
Read `dist/skillbench_export.json`. It may be large (thousands of conversations).
Start by reading a representative sample — the first 200 lines, then skip to a few
conversations in the middle and end. Get a feel for the structure and content.

### 2. Discover sensitive patterns
Scan message content across all roles (user, agent, tool) for **every** category
below. Do not rely on a fixed regex list — use your judgment to identify real
sensitive values vs. test fixtures, examples, and documentation. This taxonomy
is intentionally exhaustive; not every category will appear in every export.

#### A. Secrets & Credentials
- **API keys by provider** — OpenAI (`sk-*`), Anthropic (`sk-ant-*`), Google Cloud / Gemini, AWS (`AKIA*`), Azure, Stripe (`sk_live_*`, `pk_live_*`), Twilio, SendGrid, Slack (`xoxb-*`, `xoxp-*`, `xapp-*`), Discord bot tokens, Cloudflare, DigitalOcean, Heroku, Vercel, Netlify, Supabase, Firebase, Mapbox, Algolia, Pinecone, Cohere, Replicate, HuggingFace (`hf_*`), and any other provider-prefixed key formats
- **OAuth tokens** — access tokens, refresh tokens, client secrets
- **Bearer tokens and JWTs** — in headers, code, or logs
- **Session tokens / session IDs** — cookies, `PHPSESSID`, `connect.sid`, etc.
- **Passwords** — in code, config files, CLI args, env vars, connection strings, `.env` files, docker-compose files
- **Passphrases, PINs, security question answers**
- **Connection strings** — `postgres://`, `mysql://`, `mongodb://`, `mongodb+srv://`, `redis://`, `amqp://`, `sqlserver://` with embedded credentials
- **Private keys** — SSH (`-----BEGIN RSA PRIVATE KEY-----`), GPG, TLS/SSL, PEM, PKCS
- **Signing & encryption secrets** — HMAC keys, webhook signing secrets, AES keys, encryption passphrases
- **Service account credentials** — GCP JSON key files (`private_key`, `client_email`), AWS role credentials (`aws_secret_access_key`)
- **Package registry tokens** — npm (`npm_*`), PyPI, NuGet, RubyGems, Cargo, pub.dev
- **CI/CD secrets** — GitHub Actions secrets in logs, CircleCI, Jenkins, GitLab CI variables
- **Infrastructure secrets** — HashiCorp Vault tokens, Terraform state secrets, Kubernetes secrets (base64 in manifests), Docker registry credentials
- **2FA / recovery** — TOTP/HOTP seeds, backup/recovery codes
- **License keys** — commercial software license strings
- **Webhook URLs with embedded secrets** — Slack incoming webhooks, Stripe webhook endpoints, etc.
- **High-entropy strings** — any 20+ char alphanumeric string that looks like a credential and appears in an assignment or header context

#### B. Personal Identifiable Information (PII)
- **Names** — full legal names of people other than the export owner, especially in comments, TODOs, or error messages
- **Email addresses** — real addresses (not `@example.com`, `noreply@`, or well-known open-source committer addresses)
- **Phone numbers** — all formats: US (`(555) 123-4567`, `+1-555-123-4567`), international (`+44`, `+91`, etc.)
- **Physical / mailing addresses** — street addresses, PO boxes, zip codes in context
- **Government IDs** — SSNs (`XXX-XX-XXXX`), national ID numbers, passport numbers, driver's license numbers, tax IDs (EIN, ITIN)
- **Financial identifiers** — credit/debit card numbers (PAN), bank account numbers, routing numbers, IBAN
- **Date of birth** — especially in combination with names
- **Medical / health information** — diagnoses, prescriptions, provider names in context
- **Biometric identifiers** — fingerprint hashes, face encoding data
- **Profile URLs that reveal identity** — LinkedIn, Facebook, personal websites (when attributable to someone other than the export owner)
- **Demographic data** — race, ethnicity, religion, sexual orientation, when tied to identifiable individuals

#### C. Infrastructure & Network
- **Private IP addresses** — RFC 1918 (`10.*`, `172.16-31.*`, `192.168.*`) and RFC 6598 (`100.64-127.*`)
- **Internal hostnames and FQDNs** — `*.internal`, `*.local`, `*.corp`, company-specific domains
- **Internal URLs** — intranet, staging, dev, QA environment URLs
- **VPN endpoints** — WireGuard, OpenVPN configs with endpoints
- **DNS records / zone files** — internal DNS configurations
- **Firewall rules, security group configs** — ingress/egress rules revealing internal topology
- **Network CIDR blocks** — internal network range definitions
- **Load balancer / CDN endpoints** — internal ALB/NLB/CloudFront distributions
- **Container / pod identifiers** — Kubernetes pod names, Docker container IDs from internal clusters
- **Database endpoints** — RDS hostnames, internal Mongo/Redis/Postgres hosts
- **Message queue endpoints** — RabbitMQ, Kafka, SQS, Pub/Sub internal URLs
- **Port mappings** — internal service port assignments
- **Proxy configurations** — HTTP_PROXY, HTTPS_PROXY with internal addresses

#### D. File System & Environment
- **Home directory paths** — `/Users/username`, `/home/username`, `C:\Users\username` — these reveal the OS username
- **Project paths revealing org structure** — `/corp/team/secret-project/`
- **`.env` file contents** — even if individual values are caught, the presence of key names is informative
- **Config file paths** — `~/.aws/credentials`, `~/.ssh/config`, `~/.kube/config`, `~/.npmrc`, `~/.netrc`, `~/.gitconfig` with real content
- **Temp directory paths with usernames** — `/var/folders/xx/...`, `/tmp/user-*`
- **Log file paths and contents** — especially application logs with embedded sensitive data

#### E. Analytics, Tracking & Third-Party Service IDs
- **Product analytics** — PostHog (`phc_*`), Mixpanel tokens, Amplitude API keys, Heap analytics IDs, Hotjar site IDs
- **Error tracking** — Sentry DSNs (`https://*.ingest.sentry.io/*`), Bugsnag API keys, Rollbar tokens
- **APM & monitoring** — Datadog API/app keys, New Relic license keys, Grafana Cloud tokens, PagerDuty integration keys
- **Data pipeline** — Segment write keys, RudderStack keys, mParticle keys
- **Feature flags** — LaunchDarkly SDK keys, Split.io keys, Flagsmith keys
- **Customer messaging** — Intercom app IDs, Zendesk tokens, Freshdesk keys
- **Advertising / pixel IDs** — Google Analytics (`G-*`, `UA-*`), Facebook/Meta pixel IDs, Google Ads conversion IDs
- **Search & AI SaaS** — Algolia app IDs + API keys, Elasticsearch endpoints, Pinecone API keys

#### F. Organization & Business
- **Internal project codenames** — secret project names, unreleased product names
- **Internal org structure** — team names, department names, reporting chains
- **Internal ticket IDs in context** — Jira (`PROJ-1234`), Linear, Asana, Shortcut IDs that reveal project names
- **Internal document URLs** — Confluence, Notion, Google Docs, SharePoint links
- **Internal communication** — Slack workspace URLs, channel names, Teams links
- **Financial data** — revenue, cost, pricing, salary, budget figures
- **Customer data** — customer names, account IDs, support tickets
- **Contract / legal details** — terms, NDA-covered information
- **Proprietary algorithms** — trade secrets, proprietary logic that shouldn't be shared
- **Internal roadmap items** — unreleased features, strategic plans

#### G. Git & Version Control
- **Private repo URLs** — `git@github.com:org/private-repo.git`, `https://github.com/org/private-repo`
- **Git remotes** — revealing internal GitLab/Bitbucket/Gitea infrastructure
- **Git author info** — author emails and names (if different from export owner)
- **Signed commit key IDs** — GPG key IDs tied to identity
- **CI/CD pipeline URLs** — GitHub Actions run URLs, Jenkins build URLs with internal hostnames
- **Branch names revealing internal features** — `feature/secret-acquisition-project`

#### H. Communication & Collaboration Content
- **Pasted chat messages** — Slack, Teams, Discord messages involving third parties
- **Email content** — forwarded emails, email addresses in quoted text
- **Meeting transcripts or notes** — with participant names, discussion of sensitive topics
- **@mentions of real people** — usernames tied to real identities

#### I. Runtime, Debug & Log Data
- **Stack traces** — containing sensitive file paths, internal module names, or embedded data
- **Log output** — application logs with credentials, tokens, or PII in log lines
- **HTTP request/response captures** — with `Authorization` headers, cookies, request bodies containing secrets
- **Database query results** — real user data from SELECT statements in tool output
- **Environment variable dumps** — `env`, `printenv`, `os.environ` output
- **Core dumps / crash reports** — with memory contents

#### J. Compliance-Sensitive Data
- **HIPAA** — protected health information (PHI)
- **FERPA** — education records
- **PCI-DSS** — cardholder data, PANs
- **GDPR** — EU personal data (any data identifiable to an EU resident)
- **COPPA** — data about children under 13
- **SOX** — financial reporting data
- **Export controls** — ITAR/EAR-controlled technical data

### 3. Report findings to the user
Before making any changes, present a summary:
- How many conversations and messages you scanned
- Each category of sensitive data found, with counts and examples (redact the
  sensitive part in your examples — e.g., show `sk-r3xp...9e` not the full key)
- Ask the user if they want to proceed, or if there are additional patterns to handle

### 4. Write a sanitization script
Based on what you discovered, write a **one-off Python script** tailored to this
specific export. The script should:
- Read the input JSON
- Apply targeted redactions for the patterns you found
- Use descriptive replacement tags: `[API key elided]`, `[email elided]`,
  `[private IP elided]`, `~/` for home dirs, etc.
- Preserve test/example values (don't redact `@example.com`, `your-secret-here`, etc.)
- Print a summary of what was redacted
- Write to `dist/skillbench_export_sanitized.json`

### 5. Run and verify
Execute the script, then spot-check the output:
- Verify the redacted values are actually gone
- Check that no new false patterns were introduced
- Confirm the JSON is still valid and message counts match

### 6. Report to user
Tell the user what was redacted and where the sanitized file is. Remind them to
review it before sharing.

## Key principles
- **Intelligence over regex** — use context to distinguish real secrets from test
  fixtures, example code, and documentation.
- **Err on the side of redacting** — if you're unsure whether something is sensitive,
  redact it. False positives are better than leaked secrets.
- **User-specific** — every user's export will have different patterns. Discover
  them; don't assume a fixed list covers everything.
- **Preserve analytical value** — redact the sensitive VALUE but keep the structure.
  `password="[secret elided]"` is better than deleting the whole line, because the
  server-side analysis cares about the conversation flow, not the secret itself.
