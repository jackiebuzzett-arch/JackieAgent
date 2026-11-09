output "service_uri" {
  description = "Deployed Cloud Run HTTPS endpoint for JackieAgent"
  value       = google_cloud_run_v2_service.jackie_agent.uri
}

output "service_account_email" {
  description = "Least-privilege runtime Service Account email"
  value       = google_service_account.agent_sa.email
}
