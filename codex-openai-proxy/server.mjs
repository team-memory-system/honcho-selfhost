import http from 'node:http';
import fs from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { getModel, stream as piStream } from '@mariozechner/pi-ai';
import { getOAuthApiKey } from '@mariozechner/pi-ai/oauth';

const PORT = Number(process.env.PORT || 11435);
const AUTH_PATH = process.env.CODEX_AUTH_PATH || `${process.env.HOME}/.codex/auth.json`;
const DEFAULT_MODEL = process.env.DEFAULT_CODEX_MODEL || 'gpt-5.5';
const PROVIDER_ID = 'openai-codex';

class CodexAuthManager {
  constructor(authPath) {
    this.authPath = authPath;
    this.cached = null;
  }

  parseJwtPayload(token) {
    const parts = String(token || '').split('.');
    if (parts.length < 2) return {};
    const payload = parts[1] + '='.repeat((4 - (parts[1].length % 4 || 4)) % 4);
    return JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
  }

  async loadCreds() {
    const rawText = await fs.readFile(this.authPath, 'utf8');
    const raw = JSON.parse(rawText);
    const access = raw?.tokens?.access_token;
    const refresh = raw?.tokens?.refresh_token;
    const accountId = raw?.tokens?.account_id;
    if (!access || !refresh) {
      throw new Error(`Missing access_token/refresh_token in ${this.authPath}`);
    }
    const payload = this.parseJwtPayload(access);
    const expires = typeof payload.exp === 'number' ? payload.exp * 1000 : Date.now() + 30 * 60 * 1000;
    return {
      raw,
      creds: {
        access,
        refresh,
        expires,
        accountId,
      },
    };
  }

  async persistCreds(updatedCreds, raw) {
    const next = {
      ...raw,
      tokens: {
        ...(raw.tokens || {}),
        access_token: updatedCreds.access,
        refresh_token: updatedCreds.refresh,
        account_id: updatedCreds.accountId || raw?.tokens?.account_id || null,
      },
      last_refresh: new Date().toISOString(),
    };
    await fs.writeFile(this.authPath, `${JSON.stringify(next, null, 2)}\n`, 'utf8');
  }

  async getAccessToken() {
    if (!this.cached) {
      this.cached = await this.loadCreds();
    }
    const result = await getOAuthApiKey(PROVIDER_ID, {
      [PROVIDER_ID]: this.cached.creds,
    });
    if (!result?.apiKey) {
      throw new Error('Failed to obtain Codex access token from OAuth credentials');
    }
    const newCreds = result.newCredentials;
    const changed =
      newCreds.access !== this.cached.creds.access ||
      newCreds.refresh !== this.cached.creds.refresh ||
      newCreds.expires !== this.cached.creds.expires ||
      newCreds.accountId !== this.cached.creds.accountId;
    this.cached = { raw: this.cached.raw, creds: newCreds };
    if (changed) {
      await this.persistCreds(newCreds, this.cached.raw);
    }
    return result.apiKey;
  }
}

const authManager = new CodexAuthManager(AUTH_PATH);

function sendJson(res, statusCode, payload) {
  res.writeHead(statusCode, { 'content-type': 'application/json; charset=utf-8' });
  res.end(`${JSON.stringify(payload)}\n`);
}

async function readJsonBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString('utf8');
  return raw ? JSON.parse(raw) : {};
}

function extractTextFromContent(content) {
  if (content == null) return '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((item) => {
        if (!item) return '';
        if (typeof item === 'string') return item;
        if (item.type === 'text') return item.text || '';
        if (item.type === 'input_text') return item.text || '';
        return '';
      })
      .filter(Boolean)
      .join('\n');
  }
  if (typeof content === 'object' && typeof content.text === 'string') return content.text;
  return '';
}

function convertUserContent(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) {
    return extractTextFromContent(content);
  }
  const parts = [];
  for (const item of content) {
    if (!item) continue;
    if (item.type === 'text' || item.type === 'input_text') {
      parts.push({ type: 'text', text: item.text || '' });
      continue;
    }
    if (item.type === 'image_url' && typeof item.image_url?.url === 'string') {
      const url = item.image_url.url;
      if (url.startsWith('data:')) {
        const match = url.match(/^data:(.+?);base64,(.+)$/);
        if (match) {
          parts.push({ type: 'image', mimeType: match[1], data: match[2] });
        }
      }
    }
  }
  if (parts.length === 0) return '';
  if (parts.every((part) => part.type === 'text')) {
    return parts.map((part) => part.text).join('\n');
  }
  return parts;
}

function normalizeToolArgs(argumentsText) {
  if (!argumentsText) return {};
  if (typeof argumentsText === 'object') return argumentsText;
  try {
    return JSON.parse(argumentsText);
  } catch {
    return { _raw: String(argumentsText) };
  }
}

function augmentSystemPrompt(systemPrompt, body) {
  const extras = [];
  let basePrompt = systemPrompt?.trim() || 'You are a helpful assistant. Reply in concise plain text.';
  const rf = body.response_format;
  if (rf?.type === 'json_object') {
    extras.push('Return only a valid JSON object. Do not wrap it in markdown fences.');
  } else if (rf?.type === 'json_schema') {
    const schema = rf?.json_schema?.schema || rf?.json_schema;
    if (schema) {
      extras.push(`Return only JSON matching this schema:\n${JSON.stringify(schema)}`);
    }
  }

  if (body.tool_choice === 'required' || body.tool_choice === 'any') {
    extras.push('You must call at least one available tool before giving a final answer.');
  } else if (body.tool_choice && typeof body.tool_choice === 'object') {
    const forcedName = body.tool_choice?.function?.name || body.tool_choice?.name;
    if (forcedName) {
      extras.push(`You must call the tool \"${forcedName}\" before giving a final answer.`);
    }
  }

  const combined = [basePrompt, ...extras].filter(Boolean).join('\n\n').trim();
  return combined || undefined;
}

function convertTools(bodyTools, toolChoice) {
  if (toolChoice === 'none') return undefined;
  if (!Array.isArray(bodyTools) || bodyTools.length === 0) return undefined;
  return bodyTools
    .filter((tool) => tool?.type === 'function' && tool.function?.name)
    .map((tool) => ({
      name: tool.function.name,
      description: tool.function.description || '',
      parameters: tool.function.parameters || { type: 'object', properties: {} },
    }));
}

function convertChatRequest(body) {
  const toolNameById = new Map();
  const messages = [];
  const systemParts = [];
  let idx = 0;

  for (const message of body.messages || []) {
    const role = message?.role;
    if (role === 'system' || role === 'developer') {
      const text = extractTextFromContent(message.content);
      if (text) systemParts.push(text);
      continue;
    }

    if (role === 'user') {
      messages.push({
        role: 'user',
        content: convertUserContent(message.content),
        timestamp: Date.now() + idx++,
      });
      continue;
    }

    if (role === 'assistant') {
      const content = [];
      const text = extractTextFromContent(message.content);
      if (text) content.push({ type: 'text', text });
      for (const toolCall of message.tool_calls || []) {
        const id = toolCall.id || `call_${randomUUID()}`;
        const name = toolCall.function?.name || 'tool';
        toolNameById.set(id, name);
        content.push({
          type: 'toolCall',
          id,
          name,
          arguments: normalizeToolArgs(toolCall.function?.arguments),
        });
      }
      if (content.length > 0) {
        messages.push({
          role: 'assistant',
          content,
          api: 'openai-codex-responses',
          provider: 'openai-codex',
          model: body.model || DEFAULT_MODEL,
          usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
          stopReason: content.some((block) => block.type === 'toolCall') ? 'toolUse' : 'stop',
          timestamp: Date.now() + idx++,
        });
      }
      continue;
    }

    if (role === 'tool') {
      const toolCallId = message.tool_call_id || '';
      const toolName = message.name || toolNameById.get(toolCallId) || 'tool';
      const text = extractTextFromContent(message.content) || 'No result provided';
      messages.push({
        role: 'toolResult',
        toolCallId,
        toolName,
        content: [{ type: 'text', text }],
        isError: false,
        timestamp: Date.now() + idx++,
      });
    }
  }

  const systemPrompt = augmentSystemPrompt(systemParts.join('\n\n'), body);
  return {
    systemPrompt,
    messages,
    tools: convertTools(body.tools, body.tool_choice),
  };
}

function mapFinishReason(stopReason, toolCallCount) {
  if (stopReason === 'toolUse' || toolCallCount > 0) return 'tool_calls';
  if (stopReason === 'length') return 'length';
  return 'stop';
}

function formatAssistantMessage(assistant) {
  const text = assistant.content
    .filter((block) => block.type === 'text')
    .map((block) => block.text)
    .join('')
    .trim();
  const toolCalls = assistant.content
    .filter((block) => block.type === 'toolCall')
    .map((block) => ({
      id: block.id,
      type: 'function',
      function: {
        name: block.name,
        arguments: JSON.stringify(block.arguments ?? {}),
      },
    }));

  return {
    role: 'assistant',
    content: text || null,
    ...(toolCalls.length > 0 ? { tool_calls: toolCalls } : {}),
  };
}

function formatUsage(assistant, model) {
  const usage = {
    prompt_tokens: assistant.usage?.input ?? 0,
    completion_tokens: assistant.usage?.output ?? 0,
    total_tokens: assistant.usage?.totalTokens ?? ((assistant.usage?.input ?? 0) + (assistant.usage?.output ?? 0)),
  };
  // PROXY_USAGE_LOG가 설정돼 있으면 chat completion 1건마다 usage를 JSONL로 append(벤치 계측용).
  const usageLog = process.env.PROXY_USAGE_LOG;
  if (usageLog) {
    const line = JSON.stringify({
      ts: new Date().toISOString(),
      model: model ?? null,
      prompt_tokens: usage.prompt_tokens,
      completion_tokens: usage.completion_tokens,
    }) + '\n';
    fs.appendFile(usageLog, line).catch(() => {}); // fire-and-forget — 응답 지연 없음.
  }
  return usage;
}

function createChunkBase(id, model, created) {
  return {
    id,
    object: 'chat.completion.chunk',
    created,
    model,
  };
}

async function handleChatCompletions(req, res) {
  const body = await readJsonBody(req);
  const modelId = body.model || DEFAULT_MODEL;
  // pi-ai's static catalog can lag newly released Codex models. The Codex
  // backend itself accepts the model id, so inherit the transport metadata
  // from the known default model when a new id is not catalogued yet.
  const catalogModel = getModel(PROVIDER_ID, modelId);
  const defaultModel = getModel(PROVIDER_ID, DEFAULT_MODEL)
    || getModel(PROVIDER_ID, 'gpt-5.5');
  if (!catalogModel && !defaultModel) {
    throw new Error(`Unknown Codex model: ${modelId}`);
  }
  const model = catalogModel || { ...defaultModel, id: modelId, name: modelId };
  const context = convertChatRequest({ ...body, model: modelId });
  const apiKey = await authManager.getAccessToken();
  const stream = piStream(model, context, {
    apiKey,
    transport: 'sse',
    maxTokens: body.max_completion_tokens ?? body.max_tokens,
    temperature: body.temperature,
    reasoningEffort: body.reasoning_effort,
    sessionId: req.headers['x-session-id'] || body.user || undefined,
  });

  if (body.stream) {
    const id = `chatcmpl-${randomUUID()}`;
    const created = Math.floor(Date.now() / 1000);
    const includeUsage = Boolean(body.stream_options?.include_usage);
    let sentRole = false;
    let toolIndex = 0;

    res.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
    });

    const writeEvent = (payload) => {
      res.write(`data: ${JSON.stringify(payload)}\n\n`);
    };

    try {
      for await (const event of stream) {
        if (!sentRole && (event.type === 'text_start' || event.type === 'toolcall_start' || event.type === 'thinking_start')) {
          writeEvent({
            ...createChunkBase(id, modelId, created),
            choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }],
          });
          sentRole = true;
        }

        if (event.type === 'text_delta') {
          writeEvent({
            ...createChunkBase(id, modelId, created),
            choices: [{ index: 0, delta: { content: event.delta }, finish_reason: null }],
          });
        }

        if (event.type === 'toolcall_end') {
          if (!sentRole) {
            writeEvent({
              ...createChunkBase(id, modelId, created),
              choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }],
            });
            sentRole = true;
          }
          writeEvent({
            ...createChunkBase(id, modelId, created),
            choices: [
              {
                index: 0,
                delta: {
                  tool_calls: [
                    {
                      index: toolIndex++,
                      id: event.toolCall.id,
                      type: 'function',
                      function: {
                        name: event.toolCall.name,
                        arguments: JSON.stringify(event.toolCall.arguments ?? {}),
                      },
                    },
                  ],
                },
                finish_reason: null,
              },
            ],
          });
        }

        if (event.type === 'done') {
          const assistant = event.message;
          const finishReason = mapFinishReason(
            assistant.stopReason,
            assistant.content.filter((block) => block.type === 'toolCall').length,
          );
          writeEvent({
            ...createChunkBase(id, modelId, created),
            choices: [{ index: 0, delta: {}, finish_reason: finishReason }],
          });
          if (includeUsage) {
            writeEvent({
              ...createChunkBase(id, modelId, created),
              choices: [],
              usage: formatUsage(assistant, modelId),
            });
          }
        }
      }
      res.write('data: [DONE]\n\n');
      res.end();
      return;
    } catch (error) {
      res.write(`data: ${JSON.stringify({ error: { message: String(error?.message || error) } })}\n\n`);
      res.write('data: [DONE]\n\n');
      res.end();
      return;
    }
  }

  let assistant = null;
  const seenEvents = [];
  for await (const event of stream) {
    seenEvents.push(event.type);
    if (event.type === 'done') {
      assistant = event.message;
    }
    if (event.type === 'error') {
      console.error('[codex-proxy] non-stream error event', {
        model: modelId,
        message_roles: (body.messages || []).map((msg) => msg.role),
        event,
      });
    }
  }
  if (!assistant) {
    console.error('[codex-proxy] no assistant response', {
      model: modelId,
      message_roles: (body.messages || []).map((msg) => msg.role),
      response_format: body.response_format || null,
      seenEvents,
    });
    throw new Error('No assistant response received from Codex proxy');
  }

  sendJson(res, 200, {
    id: `chatcmpl-${randomUUID()}`,
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model: modelId,
    choices: [
      {
        index: 0,
        message: formatAssistantMessage(assistant),
        finish_reason: mapFinishReason(
          assistant.stopReason,
          assistant.content.filter((block) => block.type === 'toolCall').length,
        ),
      },
    ],
    usage: formatUsage(assistant, modelId),
  });
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/health') {
      return sendJson(res, 200, { status: 'ok', auth_path: AUTH_PATH, provider: PROVIDER_ID, default_model: DEFAULT_MODEL });
    }

    if (req.method === 'POST' && req.url === '/v1/chat/completions') {
      return await handleChatCompletions(req, res);
    }

    return sendJson(res, 404, { error: 'Not found' });
  } catch (error) {
    return sendJson(res, 500, { error: String(error?.message || error) });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(JSON.stringify({ status: 'listening', port: PORT, auth_path: AUTH_PATH, default_model: DEFAULT_MODEL }));
});
