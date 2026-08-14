/** WebSocket connection manager for real-time workflow updates. */

import type { WSMessage } from "./types";
import { getAnonymousId, getAuthToken } from "./api";

const WS_BASE = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000";

type StatusCallback = (message: WSMessage) => void;

export class ProjectWebSocket {
  private ws: WebSocket | null = null;
  private projectId: string;
  private callbacks: Set<StatusCallback> = new Set();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;
  private pingInterval: ReturnType<typeof setInterval> | null = null;

  constructor(projectId: string) {
    this.projectId = projectId;
  }

  connect() {
    if (this.ws?.readyState === WebSocket.OPEN) return;

    const params = new URLSearchParams();
    params.set("anon_id", getAnonymousId());
    const token = getAuthToken();
    if (token) params.set("token", token);

    const url = `${WS_BASE}/api/ws/${this.projectId}?${params.toString()}`;
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
      // Start ping/pong to keep connection alive
      this.pingInterval = setInterval(() => {
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: "ping" }));
        }
      }, 30000);
    };

    this.ws.onmessage = (event) => {
      try {
        const message: WSMessage = JSON.parse(event.data);
        this.notify(message);
      } catch {
        console.warn("Failed to parse WS message:", event.data);
      }
    };

    this.ws.onclose = () => {
      this.cleanup();
      if (this.reconnectAttempts < this.maxReconnectAttempts) {
        this.reconnectAttempts++;
        setTimeout(() => this.connect(), this.reconnectDelay * this.reconnectAttempts);
      }
    };

    this.ws.onerror = (error) => {
      console.error("WebSocket error:", error);
    };
  }

  private cleanup() {
    if (this.pingInterval) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }

  private notify(message: WSMessage) {
    this.callbacks.forEach((cb) => {
      try {
        cb(message);
      } catch (e) {
        console.error("WebSocket callback error:", e);
      }
    });
  }

  onStatusUpdate(callback: StatusCallback): () => void {
    this.callbacks.add(callback);
    return () => {
      this.callbacks.delete(callback);
    };
  }

  disconnect() {
    this.cleanup();
    this.maxReconnectAttempts = 0;
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.callbacks.clear();
  }
}
