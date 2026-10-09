import assert from 'node:assert/strict';
import { Client, StreamableHTTPClientTransport } from '@modelcontextprotocol/client';

const protocolVersion = '2026-07-28';
const endpoint = process.env.MCP_STATELESS_URL;
const bearer = process.env.MCP_STATELESS_TOKEN;
const toolName = process.env.MCP_STATELESS_TOOL;
const argumentsText = process.env.MCP_STATELESS_ARGUMENTS_JSON ?? '{}';
const legacySessionId = process.env.MCP_STATELESS_SESSION_ID;
const expectToolError = process.env.MCP_STATELESS_EXPECT_TOOL_ERROR === 'true';
const expectConnectFailure = process.env.MCP_STATELESS_EXPECT_CONNECT_FAILURE === 'true';

assert.ok(endpoint, 'MCP_STATELESS_URL is required');
assert.ok(bearer, 'MCP_STATELESS_TOKEN is required');
assert.ok(toolName, 'MCP_STATELESS_TOOL is required');
const toolArguments = JSON.parse(argumentsText);
const endpointUrl = new URL(endpoint);
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(endpointUrl.hostname),
  'acceptance client is restricted to a loopback endpoint');
assert.ok(['http:', 'https:'].includes(endpointUrl.protocol),
  'MCP_STATELESS_URL must use HTTP or HTTPS');

function collectProtocolTags(value, tags = []) {
  if (Array.isArray(value)) {
    for (const item of value) collectProtocolTags(item, tags);
  } else if (value && typeof value === 'object') {
    for (const [key, item] of Object.entries(value)) {
      if (key === 'mcp_protocol') tags.push(item);
      collectProtocolTags(item, tags);
    }
  }
  return tags;
}

const observedRequests = [];
const observedResponses = [];
const nativeFetch = globalThis.fetch.bind(globalThis);
globalThis.fetch = async (input, init) => {
  const request = new Request(input, init);
  let body = {};
  try {
    body = JSON.parse(await request.clone().text());
  } catch {
    // Keep an unparseable body out of the evidence record; protocol assertions fail below.
  }
  const requestSummary = {
    method: request.method,
    protocolVersionHeader: request.headers.get('mcp-protocol-version'),
    mcpMethodHeader: request.headers.get('mcp-method'),
    mcpNameHeader: request.headers.get('mcp-name'),
    authorizationPresent: request.headers.has('authorization'),
    sessionHeaderPresent: request.headers.has('mcp-session-id'),
    bodyMethod: body.method ?? null,
    bodyName: body.params?.name ?? null,
    bodyProtocolVersion: body.params?._meta?.['io.modelcontextprotocol/protocolVersion'] ?? null,
    bodyCapabilitiesPresent: Object.hasOwn(
      body.params?._meta ?? {},
      'io.modelcontextprotocol/clientCapabilities',
    ),
  };
  observedRequests.push(requestSummary);
  const response = await nativeFetch(request);
  let envelope = {};
  try {
    const responseText = await response.clone().text();
    const dataLines = responseText.split(/\r?\n/).filter((line) => line.startsWith('data: '));
    const jsonText = dataLines.length ? dataLines.at(-1).slice(6) : responseText;
    envelope = JSON.parse(jsonText ?? '{}');
  } catch {
    // Keep response content out of the evidence; record only parsed protocol metadata.
  }
  const result = envelope.result ?? {};
  observedResponses.push({
    requestMethod: requestSummary.bodyMethod,
    status: response.status,
    sessionHeaderReturned: response.headers.has('mcp-session-id'),
    cacheControl: response.headers.get('cache-control'),
    resultType: result.resultType ?? null,
    ttlMs: result.ttlMs ?? null,
    cacheScope: result.cacheScope ?? null,
    serverInfoPresent: Boolean(result._meta?.['io.modelcontextprotocol/serverInfo']),
    errorCode: envelope.error?.code ?? null,
  });
  return response;
};

const client = new Client(
  { name: 'rag-project-stateless-acceptance', version: '1.0.0' },
  { versionNegotiation: { mode: { pin: protocolVersion } } },
);
const transport = new StreamableHTTPClientTransport(endpointUrl, {
  requestInit: {
    headers: {
      Authorization: `Bearer ${bearer}`,
      ...(legacySessionId ? { 'Mcp-Session-Id': legacySessionId } : {}),
    },
  },
});

let connected = false;
try {
  await client.connect(transport);
  connected = true;
  assert.equal(client.getProtocolEra(), 'modern', 'SDK must stay on modern protocol');
  const discovery = client.getDiscoverResult();
  assert.ok(discovery, 'pinned modern connect must provide server discovery');
  assert.ok(discovery.supportedVersions.includes(protocolVersion));
  assert.ok(discovery.capabilities?.tools, 'server must advertise tools capability');

  const listed = await client.listTools();
  assert.ok(Array.isArray(listed.tools), 'tools/list must return a tools array');
  assert.equal(typeof listed.ttlMs, 'number', 'tools/list must include ttlMs');
  assert.ok(['public', 'private'].includes(listed.cacheScope),
    'tools/list must declare a valid cache scope');
  const tool = listed.tools.find((candidate) => candidate.name === toolName);
  assert.ok(tool, `tools/list did not include ${toolName}`);

  const callResult = await client.callTool({ name: toolName, arguments: toolArguments });
  assert.equal(callResult.isError, expectToolError,
    expectToolError ? 'synthetic negative call must use MCP tool error semantics'
      : 'synthetic acceptance tool call must succeed');
  assert.ok(callResult.structuredContent && typeof callResult.structuredContent === 'object',
    'tools/call must return structured content for this acceptance tool');
  assert.equal(callResult.structuredContent.mcp_protocol, protocolVersion,
    'structured result must identify the modern MCP protocol version');
  const protocolTags = collectProtocolTags(callResult.structuredContent);
  assert.ok(protocolTags.length > 0);
  assert.ok(protocolTags.every((tag) => tag === protocolVersion),
    'all structured result protocol tags must use the modern version');

  const requestMethods = observedRequests.map((request) => request.bodyMethod);
  assert.ok(requestMethods.includes('server/discover'), 'SDK must send server/discover');
  assert.ok(requestMethods.includes('tools/list'), 'SDK must send tools/list');
  assert.ok(requestMethods.includes('tools/call'), 'SDK must send tools/call');
  assert.ok(!requestMethods.includes('initialize'), 'modern client must not downgrade');
  for (const request of observedRequests) {
    assert.equal(request.method, 'POST');
    assert.equal(request.protocolVersionHeader, protocolVersion);
    assert.equal(request.bodyProtocolVersion, protocolVersion);
    assert.equal(request.mcpMethodHeader, request.bodyMethod);
    assert.equal(request.authorizationPresent, true);
    assert.equal(request.sessionHeaderPresent, Boolean(legacySessionId));
    assert.equal(request.bodyCapabilitiesPresent, true);
  }
  const callRequest = observedRequests.find((request) => request.bodyMethod === 'tools/call');
  assert.equal(callRequest?.mcpNameHeader, toolName);
  const callResponse = observedResponses.find((response) => response.requestMethod === 'tools/call');
  assert.match(callResponse?.cacheControl ?? '', /no-store/i,
    'personal retrieval results must not be stored by HTTP caches');
  for (const method of ['server/discover', 'tools/list', 'tools/call']) {
    const response = observedResponses.find((candidate) => candidate.requestMethod === method);
    assert.equal(response?.status, 200, `${method} must return HTTP 200`);
    assert.equal(response?.resultType, 'complete', `${method} must complete normally`);
    assert.equal(response?.errorCode, null);
    assert.equal(response?.serverInfoPresent, true);
    assert.equal(response?.sessionHeaderReturned, false);
  }

  process.stdout.write(`${JSON.stringify({
    clientPackage: '@modelcontextprotocol/client',
    clientVersion: '2.3.1',
    protocolVersion,
    protocolEra: client.getProtocolEra(),
    discovery: {
      supportedVersions: discovery.supportedVersions,
      capabilities: discovery.capabilities,
    },
    toolNames: listed.tools.map((candidate) => candidate.name),
    callResult,
    observedRequests,
    observedResponses,
  }, null, 2)}\n`);
} catch (error) {
  if (!expectConnectFailure) throw error;
  assert.equal(connected, false, 'expected pinning to fail during connect');
  assert.ok(observedRequests.some((request) => request.bodyMethod === 'server/discover'),
    'pinned client must probe modern discovery');
  assert.ok(!observedRequests.some((request) => request.bodyMethod === 'initialize'),
    'pinned modern client must never fall back to initialize');
  process.stdout.write(`${JSON.stringify({
    clientPackage: '@modelcontextprotocol/client',
    clientVersion: '2.3.1',
    protocolVersion,
    expectedConnectFailure: true,
    errorName: error?.name ?? 'Error',
    observedRequests,
    observedResponses,
  }, null, 2)}\n`);
} finally {
  await client.close();
}
