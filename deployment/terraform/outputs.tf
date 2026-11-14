output "cloud_run_service_url" {
  description = "Public HTTPS endpoint of the deployed Dinner Planning Agent v2"
  value       = google_cloud_run_v2_service.dinner_agent_service.uri
}

output "service_account_email" {
  description = "Runtime Service Account email"
  value       = google_service_account.dinner_agent_sa.email
}
