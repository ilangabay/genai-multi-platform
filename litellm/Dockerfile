# syntax=docker/dockerfile:1.7
# LiteLLM proxy + the IAM Roles Anywhere credential helper. Config: litellm/config.yaml. Run with compose.yaml.
FROM ghcr.io/berriai/litellm:v1.108.7

# Turns the VM certificate into AWS credentials (credential_process in litellm/aws-config). Needs glibc 2.34+.
ADD --chmod=755 --checksum=sha256:4ec587c4b2a7a4b2980f0fb084c3fabd99a862ce1091341ba514ae3c0923b905 \
    https://rolesanywhere.amazonaws.com/releases/1.8.0/X86_64/Linux/Amzn2023/aws_signing_helper /usr/local/bin/aws_signing_helper

COPY config.yaml /app/config.yaml

# Air gap: use the price map shipped in the image instead of downloading it from GitHub at startup
ENV LITELLM_LOCAL_MODEL_COST_MAP=True
EXPOSE 4000
CMD ["--config", "/app/config.yaml", "--port", "4000"]
