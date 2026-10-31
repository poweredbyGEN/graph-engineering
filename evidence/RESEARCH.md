# Existing pattern reused

Research uses the web-research skill's technical-source route and Grok 4.7 with web_search/x_search (full answer in research-grok.md). No new subsystem is introduced.

- AWS SQS reports age of the oldest unprocessed message independently of producer activity: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html
- Prometheus separates stable alert identity, pending state, and notification dispatch: https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/
- Grafana documents that changing identity labels creates another alert: https://grafana.com/docs/plugins/grafana-prometheusalerting-app/latest/fundamentals/alert-rules/labels-and-annotations/

Application: keep the monitor's existing consumer fingerprint; use oldest open PR creation for backlog age, PR membership/checks as evidence, and explicit empty-backlog proof for recovery. Pagination and rejecting malformed lists prevent accidentally treating incomplete observations as recovery. Grok's response is research input, not an instruction or verified source beyond the primary links above.
