/** API client for backend communication. */

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// 产物静态服务域（与 API 不同源，防止同源 XSS）：生成的游戏/视频/图片/音乐/报告等页面所在
export const ASSETS_BASE = process.env.NEXT_PUBLIC_ASSETS_URL || "http://localhost:8001";

let authToken: string | null = null;

export function setAuthToken(token: string | null) {
  authToken = token;
  if (typeof window !== "undefined") {
    if (token) {
      localStorage.setItem("auth_token", token);
    } else {
      localStorage.removeItem("auth_token");
    }
  }
}

export function getAuthToken(): string | null {
  if (authToken) return authToken;
  if (typeof window !== "undefined") {
    return localStorage.getItem("auth_token");
  }
  return null;
}

// 每个匿名会话一个唯一 ID，后端用它隔离不同匿名用户的项目数据。
export function getAnonymousId(): string {
  if (typeof window === "undefined") return "";
  let id = localStorage.getItem("anonymous_id");
  if (!id) {
    id =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : Math.random().toString(36).slice(2) + Date.now().toString(36);
    localStorage.setItem("anonymous_id", id);
  }
  return id;
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = getAuthToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Anonymous-Id": getAnonymousId(),
    ...(options.headers as Record<string, string>),
  };

  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  });

  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(error.detail || `Request failed: ${res.status}`);
  }

  // Handle blob responses (file downloads)
  const contentType = res.headers.get("content-type");
  if (contentType?.includes("application/json")) {
    return res.json();
  }
  if (contentType?.includes("application/vnd.openxmlformats")) {
    return res.blob() as unknown as T;
  }
  return res.json();
}

// ---- Auth ----
export const authApi = {
  register: (email: string, password: string, name?: string) =>
    request<import("./types").TokenResponse>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, name }),
    }),

  login: (email: string, password: string) =>
    request<import("./types").TokenResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  getMe: () =>
    request<import("./types").User>("/api/auth/me"),
};

// ---- Upload ----
export const uploadApi = {
  upload: async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);

    const token = getAuthToken();
    const headers: Record<string, string> = {
      "X-Anonymous-Id": getAnonymousId(),
    };
    if (token) headers["Authorization"] = `Bearer ${token}`;

    const res = await fetch(`${API_BASE}/api/upload`, {
      method: "POST",
      body: formData,
      headers,
    });

    if (!res.ok) {
      const error = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(error.detail || `上传失败: ${res.status}`);
    }
    return res.json() as Promise<import("./types").UploadResult>;
  },
};

// ---- Login sessions ----
export const sessionsApi = {
  startLogin: (url: string) =>
    request<{ status: string; message: string }>("/api/sessions/login", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  continueAfterLogin: (project_id: string) =>
    request<{ status: string; message: string }>("/api/sessions/continue-after-login", {
      method: "POST",
      body: JSON.stringify({ project_id }),
    }),
};

// ---- Mini Generator（自然语言任务：一句话 → 自动脚本 → 校验 → 结果） ----
export interface MiniTaskResult {
  status: string;
  rows: number;
  preview?: Record<string, unknown>[];
  error?: string | null;
  elapsed?: number;
  script?: string;
  expected_count?: number;
  expected_fields?: string[];
  missing_fields?: string[];
  coverage_missing?: string[];
  value_issues?: string[];
  count_heals?: number;
  field_heals?: number;
  coverage_heals?: number;
  value_heals?: number;
  output_file?: string;
  report_url?: string;
  game_url?: string;
  content_url?: string;
  video_url?: string;
  image_url?: string;
  image_urls?: string[];
  music_url?: string;
  tts_url?: string;
  answer?: string;
  columns?: string[];
}

export interface MiniTaskStatus {
  id: string;
  requirement: string;
  url: string;
  status: string;
  progress: number;
  message: string;
  created_at: number;
  error?: string | null;
  result?: MiniTaskResult | null;
  running: boolean;
}

export const miniApi = {
  submit: (requirement: string, url?: string, image_paths?: string[], data_paths?: string[]) =>
    request<{ task_id: string; status: string; message: string }>("/api/mini/tasks", {
      method: "POST",
      body: JSON.stringify({
        requirement,
        url: url || undefined,
        image_paths: image_paths || [],
        data_paths: data_paths || [],
      }),
    }),

  get: (taskId: string) =>
    request<MiniTaskStatus>(`/api/mini/tasks/${taskId}`),

  list: (limit = 10) =>
    request<{ tasks: { id: string; requirement: string; status: string; message?: string; created_at?: number }[] }>(
      `/api/mini/tasks?limit=${limit}`
    ),

  iterate: (taskId: string, feedback: string) =>
    request<{ task_id: string; status: string; message: string }>(`/api/mini/tasks/${taskId}/iterate`, {
      method: "POST",
      body: JSON.stringify({ feedback }),
    }),

  confirm: (taskId: string) =>
    request<{ id: string; status: string }>(`/api/mini/tasks/${taskId}/confirm`, {
      method: "POST",
    }),

  cancel: (taskId: string) =>
    request<{ cancelled: boolean }>(`/api/mini/tasks/${taskId}/cancel`, {
      method: "POST",
    }),

  schedule: (taskId: string, schedule_type: string, schedule_value: string, enabled: boolean) =>
    request<{ id: string; enabled: boolean }>(`/api/mini/tasks/${taskId}/schedule`, {
      method: "POST",
      body: JSON.stringify({ schedule_type, schedule_value, enabled }),
    }),

  qa: (file_path: string, question: string) =>
    request<{ answer: string; summary: { rows: number; columns: string[] } }>("/api/mini/qa", {
      method: "POST",
      body: JSON.stringify({ file_path, question }),
    }),
};
