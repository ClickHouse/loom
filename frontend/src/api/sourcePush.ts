import { post } from "../api";

type Source = { org: string; namespace: string; name: string };

export type PushStatus = {
  enabled: boolean;
  status: string;
  provider: string;
  generation: string;
  callback_url: string;
  challenge_fingerprint: string;
  verification_token: string;
  channels: number;
  expires_at: number;
};

export function sourcePushStatus(source: Source, reveal = false, signal?: AbortSignal): Promise<PushStatus> {
  return post("/v1/knowledge.source.push_status", { ...source, reveal_challenge: reveal }, signal);
}

export function configureSourcePush(source: Source, enabled: boolean): Promise<PushStatus> {
  return post("/v1/knowledge.source.push_configure", { ...source, enabled });
}

export function confirmSourcePush(source: Source, status: PushStatus, secret = ""): Promise<PushStatus> {
  return post("/v1/knowledge.source.push_confirm", {
    ...source,
    generation: status.generation,
    challenge_fingerprint: status.challenge_fingerprint,
    signing_secret: secret,
  });
}
