# AWS setup: Bedrock through STS roles

How the gateway VM gets into AWS. No long-lived AWS key is stored anywhere.

```
VM certificate ──► IAM Roles Anywhere ──► role genai-gateway-vm ──(STS AssumeRole)──► role genai-gateway-bedrock ──► Bedrock
 (certs/)          aws_signing_helper     can only assume the next role               can only invoke the Claude models
```

1. `aws_signing_helper`, inside the LiteLLM container, signs a request with the VM's private key. IAM Roles Anywhere
   returns 1-hour credentials for `genai-gateway-vm`.
2. LiteLLM calls `sts:AssumeRole` into `genai-gateway-bedrock` (`BEDROCK_ROLE_ARN`) and calls Bedrock with that role.
   Both sets of credentials are refreshed before they expire.

Replace `111122223333` with your account ID and `eu-west-3` if you use another region. Every step is in the AWS console
(or the CLI from a connected machine). The VM itself only needs the three hosts at the end.

## 1. Enable the models

Bedrock console, region **eu-west-3 (Paris)**: open each of Claude Opus 5.5, Sonnet 5.5 and Haiku 5.5 in the playground
and send one message, as an admin. The first call per account accepts the Anthropic terms (use-case form) and the
Marketplace subscription, so the gateway role does not need Marketplace permissions.

## 2. Certificate for the VM

Use your internal PKI if you have one: you need the **CA certificate** (for AWS) and, for the VM, a **certificate and
private key** signed by that CA (`keyUsage = digitalSignature`, RSA 2048+ or EC P-256, SHA-256).

For a prototype, a private CA made with openssl works:

```bash
mkdir certs && cd certs
openssl req -x509 -newkey rsa:3072 -nodes -days 1825 -keyout ca.key -out ca.crt -subj "/CN=genai-gateway-ca" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
openssl req -newkey rsa:3072 -nodes -keyout gateway.key -out gateway.csr -subj "/CN=genai-gateway-vm"
openssl x509 -req -in gateway.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 365 -out gateway.crt \
  -extfile <(printf "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature")
```

Keep `ca.key` off the VM (it can issue new identities). The VM needs `certs/gateway.crt` and `certs/gateway.key` only;
`chmod 600 certs/gateway.key`. Renew `gateway.crt` before it expires (365 days here).

## 3. IAM Roles Anywhere trust anchor

IAM console > Roles Anywhere (region eu-west-3) > **Create a trust anchor**: source "External certificate bundle",
paste `ca.crt`. Note its ARN.

## 4. Role `genai-gateway-vm` (the VM's identity)

Trust policy (only certificates with CN `genai-gateway-vm` from your trust anchor):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "rolesanywhere.amazonaws.com"},
    "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
    "Condition": {
      "StringEquals": {"aws:PrincipalTag/x509Subject/CN": "genai-gateway-vm"},
      "ArnEquals": {"aws:SourceArn": "arn:aws:rolesanywhere:eu-west-3:111122223333:trust-anchor/TRUST_ANCHOR_ID"}
    }
  }]
}
```

Permissions: only the next hop.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
    "Resource": "arn:aws:iam::111122223333:role/genai-gateway-bedrock"
  }]
}
```

Then Roles Anywhere > **Create a profile** with this role (session duration 1 hour). Note the profile ARN.

## 5. Role `genai-gateway-bedrock` (what LiteLLM uses for Bedrock)

Trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::111122223333:role/genai-gateway-vm"},
    "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"]
  }]
}
```

Permissions: invoking models, nothing else (`Converse` and streaming use these two actions).
Admins add models to the catalog from the UI, so the role decides which ones can actually be called.
Either list each model the platform may use (strictest: every new catalog model then needs a policy change too):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": [
      "arn:aws:bedrock:eu-west-3::foundation-model/anthropic.claude-opus-5-5",
      "arn:aws:bedrock:eu-west-3::foundation-model/anthropic.claude-sonnet-5-5",
      "arn:aws:bedrock:eu-west-3::foundation-model/anthropic.claude-haiku-5-5"
    ]
  }]
}
```

or allow every model in the region and let the Catalog page be the control (`"Resource": "arn:aws:bedrock:eu-west-3::foundation-model/*"`).

For an EU inference profile (`eu.anthropic...`), add the profile
(`arn:aws:bedrock:eu-west-3:111122223333:inference-profile/eu.anthropic.claude-sonnet-5-5`) **and** the model in every
EU region the profile may route to (`arn:aws:bedrock:eu-*::foundation-model/anthropic.claude-sonnet-5-5`):
calls then leave eu-west-3, but stay in the EU. Each new model also needs step 1 (one playground message).

Session duration: keep the default 1 hour (role chaining caps it at 1 hour anyway).

## 6. On the VM

- `certs/gateway.crt` and `certs/gateway.key` next to `compose.yaml`.
- `litellm/aws-config`: copy `litellm/aws-config.example`, put in the trust anchor, profile and `genai-gateway-vm` ARNs.
- `.env`: `BEDROCK_ROLE_ARN=arn:aws:iam::111122223333:role/genai-gateway-bedrock`, plus `POSTGRES_PASSWORD`,
  `ADMIN_PASSWORD`, `LITELLM_MASTER_KEY` (`sk-` + a long random string), `LITELLM_SALT_KEY` (long random string,
  never changed afterwards) and `LITELLM_BASE_URL`
  (the LiteLLM address as users reach it, e.g. `http://gateway-vm:4000/v1`).
- Proxy allowlist, HTTPS 443:
  `rolesanywhere.eu-west-3.amazonaws.com`, `sts.eu-west-3.amazonaws.com`, `bedrock-runtime.eu-west-3.amazonaws.com`
  (LiteLLM container), and `pricing.us-east-1.amazonaws.com` (governance app: public AWS price list, read when an
  admin adds a Bedrock model and every `PRICE_SYNC_HOURS`; GET only, no credentials, no data sent).
- `docker compose up --build -d`

## 7. Check

```bash
# 1st hop: the certificate gives credentials for genai-gateway-vm (prints JSON with an Expiration)
docker compose exec litellm sh -c 'aws_signing_helper credential-process $(grep -o -- "--certificate.*" /aws/config)'
# Whole chain: a real Bedrock call through LiteLLM with the master key (costs a fraction of a cent)
curl http://localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" -d '{"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "Hello"}]}'
```

| Error in `docker compose logs litellm` | Cause |
|---|---|
| `AccessDeniedException` from Roles Anywhere / `credential-process` fails | Certificate not signed by the trust anchor CA, CN not `genai-gateway-vm`, or profile not linked to the role |
| `not authorized to perform: sts:AssumeRole` | Trust policy of `genai-gateway-bedrock` or permissions of `genai-gateway-vm` |
| `AccessDeniedException ... bedrock:InvokeModel` | Permissions of `genai-gateway-bedrock`, or a model ID that is not in its list |
| `Model use case details have not been submitted` / Marketplace error | Step 1 not done for that model |
| Timeout | Proxy allowlist |
