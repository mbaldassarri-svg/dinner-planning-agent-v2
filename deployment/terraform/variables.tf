variable "project_id" {
  description = "Google Cloud Project ID"
  type        = string
}

variable "region" {
  description = "Google Cloud region for Cloud Run deployment"
  type        = string
  default     = "us-central1"
}

variable "container_image" {
  description = "Artifact Registry container image URI for dinner-planning-agent-v2"
  type        = string
}
