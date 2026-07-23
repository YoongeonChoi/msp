#!/usr/bin/env bash

set -euo pipefail

common_secret_files="$(
  git grep -I -l -E \
    '(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC |OPENSSH |PRIVATE )?PRIVATE KEY-----)' \
    -- ':!package-lock.json' ':!**/package-lock.json' ':!docs/API_CONNECTIONS.md' || true
)"
export LC_ALL=C
protected_secret_name_pattern='(SUPABASE_SECRET_KEY|TOSS_CLIENT_SECRET|OPENAI_API_KEY|NAVER_CLIENT_SECRET|KRX_API_KEY|OPENDART_API_KEY|ALERT_WEBHOOK_URL|ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64|ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64|DEAD_MAN_ALERT_WEBHOOK_URL|DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64|DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64|RENDER_DEPLOY_HOOK_URL|SUPABASE_LIVE_REQUESTER_JWT|SUPABASE_LIVE_REVIEWER_JWT)'
literal_first_pattern='[A-Za-z0-9_./+=:@?%&~#-]'
single_quoted_first_pattern='[A-Za-z0-9_./+=:@?%&$~#-]'
literal_rest_pattern='[A-Za-z0-9_./+=:@?%&$~#!-]{15,}'
protected_secret_key_pattern="(\"${protected_secret_name_pattern}\"|'${protected_secret_name_pattern}'|${protected_secret_name_pattern})"
shell_assignment_lead_pattern='(^|[[:space:];&|({])'
shell_assignment_boundary_pattern='([[:space:]]*(;|&&|\|\||&|\|)[[:space:]]*|[[:space:]]+|$)'
quoted_secret_assignment_pattern="(^|[^[:alnum:]_])${protected_secret_key_pattern}[[:space:]]*[:=][[:space:]]*(\"${literal_first_pattern}[^<>\"[:space:]]{15,}\"|'${single_quoted_first_pattern}[^<>'[:space:]]{15,}')"
quoted_shell_secret_assignment_pattern="[\"']${protected_secret_name_pattern}[+]?=${literal_first_pattern}${literal_rest_pattern}[\"']"
env_secret_assignment_pattern="${shell_assignment_lead_pattern}${protected_secret_name_pattern}[+]?=${literal_first_pattern}${literal_rest_pattern}${shell_assignment_boundary_pattern}"
yaml_secret_assignment_pattern="(^[[:space:]]*|[{,][[:space:]]*)${protected_secret_key_pattern}[[:space:]]*:[[:space:]]*${literal_first_pattern}${literal_rest_pattern}([[:space:]]*(,|})|[[:space:]]*(#.*)?$)"
yaml_block_secret_assignment_pattern="^[[:space:]]*${protected_secret_key_pattern}[[:space:]]*:[[:space:]]*[>|][+-]?[0-9]?[[:space:]]*(#.*)?$"
yaml_indirect_secret_assignment_pattern="(^[[:space:]]*|[{,][[:space:]]*)${protected_secret_key_pattern}[[:space:]]*:[[:space:]]*[!&*][^[:space:]#,}]*"
yaml_multiline_secret_assignment_pattern="^[[:space:]]*${protected_secret_key_pattern}[[:space:]]*:[[:space:]]*(#.*)?$"

# Mapping-key indirection can change scanner semantics, so reject it fail closed.
yaml_escaped_key_pattern='(^[[:space:]]*|[{,][[:space:]]*)"[^"]*\\[^"]*"[[:space:]]*:'
yaml_noncanonical_key_prefix_pattern='(^[[:space:]]*|[{,][[:space:]]*)([!&][^[:space:]#,}]+[[:space:]]+|[*][^[:space:]#,}:]+[[:space:]]*:|[?][[:space:]]+)'

secret_scan_probe_value="abcdefgh""ijklmnop"
url_secret_probe_value='https://alerts.example.test/events?tenant=blue&route=abcdefgh'
dollar_url_secret_probe_value='https://alerts.example.test/events?route=abc$defghijklmnop'
leading_dollar_probe_value='$literal-dollar-abcdefgh'
single_quote="'"
quoted_secret_probe="$(printf '\"%s\": \"%s\"' ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64 "$secret_scan_probe_value")"
quoted_url_secret_probe="$(printf '\"%s\": \"%s\"' ALERT_WEBHOOK_URL "$url_secret_probe_value")"
quoted_dollar_secret_probe="$(printf '\"%s\": \"%s\"' ALERT_WEBHOOK_URL "$dollar_url_secret_probe_value")"
single_quoted_dollar_probe="$(printf '\"%s\": %s%s%s' ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64 "$single_quote" "$leading_dollar_probe_value" "$single_quote")"
indented_env_secret_probe="  ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value"
indented_export_secret_probe="  export ALERT_WEBHOOK_URL=$url_secret_probe_value"
local_secret_probe="  local ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value"
readonly_secret_probe="  readonly ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value"
semicolon_secret_probe="  ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value; true"
command_secret_probe="  ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value true"
prior_assignment_secret_probe="FOO=x ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value true"
multi_export_secret_probe="export FOO=x ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value"
command_env_secret_probe="command env ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value true"
subshell_secret_probe="(ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value true)"
quoted_export_secret_probe="export \"ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value\""
quoted_env_secret_probe="env \"ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value\" true"
quoted_declare_secret_probe="declare \"ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$secret_scan_probe_value\""
append_secret_probe="ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64+=$secret_scan_probe_value"
unquoted_yaml_url_secret_probe="  ALERT_WEBHOOK_URL: $url_secret_probe_value"
flow_yaml_url_secret_probe="{ ALERT_WEBHOOK_URL: $url_secret_probe_value }"
block_yaml_secret_probe='  ALERT_WEBHOOK_URL: >-'
tagged_yaml_secret_probe="  ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64: !!str $secret_scan_probe_value"
multiline_yaml_secret_probe='  ALERT_WEBHOOK_URL:'
unicode_yaml_secret_probe='  "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B6\u0034": abcdefghijklmnop'
hex_yaml_secret_probe='  "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B6\x34": abcdefghijklmnop'
long_unicode_yaml_secret_probe='  "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B6\U00000034": abcdefghijklmnop'
explicit_yaml_secret_probe='? ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64'
tagged_yaml_key_secret_probe='  !!str ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64: abcdefghijklmnop'
anchored_yaml_key_secret_probe='  &receiver-key ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64: abcdefghijklmnop'
alias_yaml_key_probe='  *receiver-key: abcdefghijklmnop'
unicode_yaml_value_probe='  display: "release\u0034"'
github_expression_suffix='{{ env.ALERT_WEBHOOK_URL }}'
yaml_github_reference_probe="$(printf '  %s: %s%s' ALERT_WEBHOOK_URL '$' "$github_expression_suffix")"

positive_probes=(
  "$quoted_secret_probe"
  "$quoted_url_secret_probe"
  "$quoted_dollar_secret_probe"
  "$single_quoted_dollar_probe"
  "$indented_env_secret_probe"
  "$indented_export_secret_probe"
  "$local_secret_probe"
  "$readonly_secret_probe"
  "$semicolon_secret_probe"
  "$command_secret_probe"
  "$prior_assignment_secret_probe"
  "$multi_export_secret_probe"
  "$command_env_secret_probe"
  "$subshell_secret_probe"
  "$quoted_export_secret_probe"
  "$quoted_env_secret_probe"
  "$quoted_declare_secret_probe"
  "$append_secret_probe"
  "$unquoted_yaml_url_secret_probe"
  "$flow_yaml_url_secret_probe"
  "$block_yaml_secret_probe"
  "$tagged_yaml_secret_probe"
  "$multiline_yaml_secret_probe"
  "$unicode_yaml_secret_probe"
  "$hex_yaml_secret_probe"
  "$long_unicode_yaml_secret_probe"
  "$explicit_yaml_secret_probe"
  "$tagged_yaml_key_secret_probe"
  "$anchored_yaml_key_secret_probe"
  "$alias_yaml_key_probe"
)
negative_probes=(
  '"ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": "$previous_key_b64"'
  '"ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": "${PREVIOUS_KEY_B64}"'
  '  export ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=$previous_key_b64'
  "$yaml_github_reference_probe"
  "$unicode_yaml_value_probe"
  '"ALERT_WEBHOOK_URL": "<https-receiver-url>"'
  '  ALERT_WEBHOOK_URL=https://<approved-receiver>/events'
  '  ALERT_WEBHOOK_URL: https://<approved-receiver>/events'
  '"NOT_ALERT_WEBHOOK_URL": "abcdefghijklmnop"'
)
non_yaml_negative_probes=(
  '    "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": previous_key_b64,'
)

for probe in "${positive_probes[@]}"; do
  if ! grep -E -q \
    -e "$quoted_secret_assignment_pattern" \
    -e "$quoted_shell_secret_assignment_pattern" \
    -e "$env_secret_assignment_pattern" \
    -e "$yaml_secret_assignment_pattern" \
    -e "$yaml_block_secret_assignment_pattern" \
    -e "$yaml_indirect_secret_assignment_pattern" \
    -e "$yaml_multiline_secret_assignment_pattern" \
    -e "$yaml_escaped_key_pattern" \
    -e "$yaml_noncanonical_key_prefix_pattern" <<< "$probe"; then
    echo "::error::Secret assignment scanner contract self-check failed."
    exit 1
  fi
done
for probe in "${negative_probes[@]}"; do
  if grep -E -q \
    -e "$quoted_secret_assignment_pattern" \
    -e "$quoted_shell_secret_assignment_pattern" \
    -e "$env_secret_assignment_pattern" \
    -e "$yaml_secret_assignment_pattern" \
    -e "$yaml_block_secret_assignment_pattern" \
    -e "$yaml_indirect_secret_assignment_pattern" \
    -e "$yaml_multiline_secret_assignment_pattern" \
    -e "$yaml_escaped_key_pattern" \
    -e "$yaml_noncanonical_key_prefix_pattern" <<< "$probe"; then
    echo "::error::Secret assignment scanner contract self-check failed."
    exit 1
  fi
done
for probe in "${non_yaml_negative_probes[@]}"; do
  if grep -E -q \
    -e "$quoted_secret_assignment_pattern" \
    -e "$quoted_shell_secret_assignment_pattern" \
    -e "$env_secret_assignment_pattern" <<< "$probe"; then
    echo "::error::Secret assignment scanner contract self-check failed."
    exit 1
  fi
done

secret_assignment_files="$(
  {
    git grep -I -l -E \
      -e "$quoted_secret_assignment_pattern" \
      -e "$quoted_shell_secret_assignment_pattern" \
      -e "$env_secret_assignment_pattern" -- . || true
    git grep -I -l -E \
      -e "$yaml_secret_assignment_pattern" \
      -e "$yaml_block_secret_assignment_pattern" \
      -e "$yaml_indirect_secret_assignment_pattern" \
      -e "$yaml_multiline_secret_assignment_pattern" \
      -e "$yaml_escaped_key_pattern" \
      -e "$yaml_noncanonical_key_prefix_pattern" \
      -- '*.yml' '*.yaml' || true
  } | LC_ALL=C sort -u
)"
findings="$(printf '%s\n%s\n' "$common_secret_files" "$secret_assignment_files" | awk 'NF' | sort -u)"
if [[ -n "$findings" ]]; then
  echo "::error::Potential secret material found in tracked files. Only file paths are printed."
  printf '%s\n' "$findings"
  exit 1
fi
