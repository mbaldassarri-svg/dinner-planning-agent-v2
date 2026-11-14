terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_service_account" "dinner_agent_sa" {
  account_id   = "dinner-agent-v2-sa"
  display_name = "Least-Privilege Service Account for Dinner Planning Agent v2"
}

resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "dinner-agent-v2-gemini-api-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "secret_accessor" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.dinner_agent_sa.email}"
}

resource "google_project_iam_member" "vertex_ai_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.dinner_agent_sa.email}"
}

resource "google_cloud_run_v2_service" "dinner_agent_service" {
  name     = "dinner-planning-agent-v2"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.dinner_agent_sa.email

    containers {
      image = var.container_image
      ports {
        container_port = 8080
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name = "GEMINI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }
}
