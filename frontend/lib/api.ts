export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface Quote {
  start_sec: number;
  end_sec: number;
  text: string;
}

export interface Idea {
  title: string;
  hook: string;
  summary: string;
  verse_ref: string;
  target_length_sec: number;
  why_it_works: string;
  rank: number;
  quotes: Quote[];
}

export interface EpisodeIdea extends Idea {
  has_script: boolean;
  script_version: string | null;
  render_status: string | null;
  render_mp4_key: string | null;
}

export interface SceneAudio {
  index: number;
  audio_key: string;
  audio_url: string;
  source?: string;
}

export interface ScriptResponse extends Screenplay {
  scene_audio: SceneAudio[];
}

export interface Scene {
  start: number;
  end: number;
  voiceover: string;
  on_screen_text: string;
  visual: string;
  broll_query: string;
  source_start?: number | null;
  source_end?: number | null;
}

export interface Screenplay {
  title: string;
  duration_sec: number;
  aspect: string;
  scenes: Scene[];
  caption: string;
  hashtags: string[];
}

export interface EpisodeSummary {
  episode_id: string;
  name: string;
  status: string;
  created_at: string;
}

export interface EpisodeDetail {
  episode_id: string;
  name: string;
  status: string;
  created_at: string;
  ideas: EpisodeIdea[];
}

async function req<T>(
  path: string,
  opts: RequestInit = {},
): Promise<T> {
  const r = await fetch(`${API_URL}${path}`, {
    headers: { "content-type": "application/json" },
    ...opts,
  });
  if (!r.ok) throw new Error(`${path} ${r.status}: ${await r.text()}`);
  return r.json();
}

export const api = {
  uploadUrl: (episodeNumber: number, filename: string) =>
    req<{ url: string; audio_key: string; content_type: string }>(
      "/episodes/upload-url",
      { method: "POST", body: JSON.stringify({ episode_number: episodeNumber, filename }) },
    ),
  uploadFile: async (url: string, contentType: string, file: File) => {
    const r = await fetch(url, {
      method: "PUT",
      headers: { "content-type": contentType },
      body: file,
    });
    if (!r.ok) throw new Error(`upload failed: ${r.status} ${await r.text()}`);
  },
  createEpisode: (
    episodeNumber: number,
    title: string,
    audioKey: string,
  ) =>
    req<{ episode_id: string; episode_number: number; name: string; status: string }>(
      "/episodes",
      {
        method: "POST",
        body: JSON.stringify({
          episode_number: episodeNumber,
          title,
          audio_key: audioKey,
        }),
      },
    ),
  episodeStatus: (id: string) =>
    req<{ status: string; failure_reason?: string }>(`/episodes/${id}/status`),
  ideate: (id: string) =>
    req<{ episode_id: string; status: string; ideas?: Idea[] }>(
      `/episodes/${id}/ideate`,
      { method: "POST" },
    ),
  listEpisodes: () =>
    req<{ episodes: EpisodeSummary[] }>("/episodes"),
  getEpisode: (id: string) =>
    req<EpisodeDetail>(`/episodes/${id}`),
  getScript: (id: string, rank: number) =>
    req<ScriptResponse | { status: string; version?: string; failure_reason?: string }>(
      `/episodes/${id}/ideas/${rank}/script`,
    ),
  generateScript: (id: string, rank: number) =>
    req<{ episode_id: string; rank: number; version: string; status: string }>(
      `/episodes/${id}/ideas/${rank}/script`,
      { method: "POST" },
    ),
  revise: (id: string, rank: number, instruction: string) =>
    req<{ episode_id: string; rank: number; version: string; status: string }>(
      `/episodes/${id}/ideas/${rank}/revise`,
      { method: "POST", body: JSON.stringify({ instruction }) },
    ),
  scriptStatus: (id: string, rank: number) =>
    req<{ status: string; version?: string; kind?: string; failure_reason?: string }>(
      `/episodes/${id}/ideas/${rank}/script-status`,
    ),
  render: (id: string, rank: number) =>
    req<{ execution_arn: string; status: string; version: string }>(
      `/episodes/${id}/ideas/${rank}/render`,
      { method: "POST" },
    ),
  renderStatus: (id: string, rank: number) =>
    req<{ status: string; mp4_key?: string; version?: string }>(
      `/episodes/${id}/ideas/${rank}/render-status`,
    ),
  assetUrl: (key: string) =>
    req<{ url: string }>(`/assets/url?key=${encodeURIComponent(key)}`),
};
