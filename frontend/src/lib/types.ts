/** Core types for the AI Automation Generator frontend. */

export interface User {
  id: string;
  email: string;
  name: string | null;
  credits: number;
  subscription_tier: string;
  created_at: string;
}

export type ProjectStatus =
  | "draft"
  | "generating"
  | "testing"
  | "healing"
  | "validating"
  | "previewing"
  | "running"
  | "completed"
  | "failed"
  | "iterative_modifying";

export type WorkflowStage =
  | "generating"
  | "testing"
  | "healing"
  | "validating"
  | "previewing"
  | "running"
  | "completed"
  | "failed";

export interface Project {
  id: string;
  user_id: string;
  name: string;
  target_url: string;
  requirement: string;
  status: ProjectStatus;
  created_at: string;
  updated_at: string;
}

export interface ProjectStatusResponse {
  id: string;
  status: string;
  stage: WorkflowStage | null;
  message: string | null;
  progress: number;
}

export interface PreviewData {
  columns: string[];
  rows: string[][];
  total_estimate: number;
  execution_time: number;
  fields?: string[];
  error?: string;
}

export interface ScriptVersion {
  id: string;
  project_id: string;
  version_number: number;
  script_code: string;
  source: string;
  parent_version_id: string | null;
  created_at: string;
}

export interface Execution {
  id: string;
  script_version_id: string;
  execution_type: "preview" | "full";
  status: string;
  error_log: string | null;
  result_preview: PreviewData | null;
  result_file_path: string | null;
  executed_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export interface WSMessage {
  type: "connected" | "status_update" | "pong";
  project_id: string;
  stage?: WorkflowStage;
  message?: string;
  progress?: number;
  detail?: string;
  preview_data?: PreviewData;
  script_code?: string;
}

export type ScheduleType = "interval" | "daily";

export interface ScheduledTask {
  id: string;
  user_id: string;
  requirement: string;
  script_code: string;
  schedule_type: ScheduleType;
  schedule_value: string;
  enabled: number;
  last_run_at: string | null;
  next_run_at: string | null;
  created_at: string;
}

export interface UploadResult {
  filename: string;
  path: string;
  size: number;
}
