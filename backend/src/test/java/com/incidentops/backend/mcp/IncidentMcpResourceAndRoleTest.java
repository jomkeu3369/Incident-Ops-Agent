package com.incidentops.backend.mcp;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.incidentops.backend.config.InternalRoleContext;
import com.incidentops.backend.config.InternalTokenFilter;
import com.incidentops.backend.domain.Incident;
import com.incidentops.backend.mapper.CorpusMapper;
import com.incidentops.backend.service.IncidentService;
import io.modelcontextprotocol.server.McpServerFeatures;
import io.modelcontextprotocol.spec.McpSchema;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Validation;
import jakarta.validation.Validator;
import java.util.List;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

class IncidentMcpResourceAndRoleTest {

    @AfterEach
    void tearDown() {
        InternalRoleContext.clear();
    }

    @Test
    void exposesVersionedReadOnlyEvidenceSummaryResource() throws Exception {
        IncidentService incidentService = mock(IncidentService.class);
        Incident inc = new Incident();
        inc.setIncidentCode("INC-2026-001");
        inc.setTitle("결제\nAPI\u202E 502 급증" + "x".repeat(150));
        inc.setServiceName("payment-service");
        inc.setSeverity("SEV1");
        inc.setStatus("OPEN");
        inc.setSummary("커넥션 풀 고갈");
        when(incidentService.getAllIncidents()).thenReturn(List.of(inc));

        ObjectMapper mapper = new ObjectMapper();
        List<McpServerFeatures.SyncResourceSpecification> specs =
                new IncidentMcpConfig().incidentMcpResources(incidentService, mapper);

        assertThat(specs).hasSize(1);
        McpServerFeatures.SyncResourceSpecification spec = specs.get(0);
        assertThat(spec.resource().uri()).isEqualTo(IncidentMcpConfig.EVIDENCE_SUMMARY_URI);
        assertThat(spec.resource().mimeType()).isEqualTo("application/json");

        McpSchema.ReadResourceResult result = spec.readHandler()
                .apply(null, new McpSchema.ReadResourceRequest(IncidentMcpConfig.EVIDENCE_SUMMARY_URI));
        assertThat(result.contents()).hasSize(1);
        McpSchema.TextResourceContents textContent = (McpSchema.TextResourceContents) result.contents().get(0);

        JsonNode root = mapper.readTree(textContent.text());
        assertThat(root.get("schemaVersion").asText()).isEqualTo("v1");
        assertThat(root.get("readOnly").asBoolean()).isTrue();
        assertThat(root.get("incidentCount").asInt()).isEqualTo(1);
        JsonNode item = root.get("incidents").get(0);
        assertThat(item.get("incidentCode").asText()).isEqualTo("INC-2026-001");
        assertThat(item.get("title").asText()).startsWith("결제API 502 급증").hasSize(120);
        assertThat(item.fieldNames()).toIterable()
                .containsExactlyInAnyOrder("incidentCode", "title", "serviceName", "severity", "status");
        assertThat(textContent.text()).doesNotContain(inc.getSummary());
    }

    @Test
    void dedicatedInvestigatorCredentialOverridesForgedRoleForRestAndMcp() throws Exception {
        InternalTokenFilter filter = new InternalTokenFilter("shared-token", "test-read-only-token");
        MockHttpServletRequest read = request("GET", "/api/incidents/INC-1", "test-read-only-token", "supervisor");
        filter.doFilter(read, new MockHttpServletResponse(), (req, res) ->
                assertThat(InternalRoleContext.isReadOnlyInvestigator()).isTrue());

        for (String path : List.of("/api/incidents/INC-1/complete", "/mcp/message")) {
            MockHttpServletRequest write = request("POST", path, "test-read-only-token", "supervisor");
            write.setContent("""
                    {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"completeIncidentWithPostMortem"}}
                    """.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            MockHttpServletResponse response = new MockHttpServletResponse();
            MockFilterChain chain = new MockFilterChain();
            filter.doFilter(write, response, chain);
            assertThat(response.getStatus()).isEqualTo(HttpServletResponse.SC_FORBIDDEN);
            assertThat(chain.getRequest()).isNull();
        }
        assertThat(InternalRoleContext.getRole()).isNull();
    }

    @Test
    void investigatorTokenMustDifferFromSharedToken() {
        assertThatThrownBy(() -> new InternalTokenFilter("same-token", "same-token"))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("INTERNAL_INVESTIGATOR_TOKEN");
    }

    private static final String WRITE_CALL = "\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"tools/call\","
            + "\"params\":{\"name\":\"completeIncidentWithPostMortem\",\"arguments\":{}}";
    private static final String PING = "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"ping\"}";

    @Test
    void investigatorMcpRejectsBodiesTheSdkCouldDecodeDifferently() throws Exception {
        // UTF-8 바이트를 GBK로 읽으면 첫 문자열이 일찍 닫혀 tools/call이 드러나는 본문
        String smuggled = "{\"a\":\"\u4E01\\\",\"}\":1," + WRITE_CALL + "}";
        for (String contentType : List.of(
                "application/json; charset=GBK",
                "application/json; charset=\"GBK\"",
                "application/json; charset=Shift_JIS",
                "application/json; charset=UTF-8; charset=GBK",
                "text/plain",
                "not a media type")) {
            assertThat(investigatorMcpStatus(smuggled, contentType)).as(contentType).isEqualTo(HttpServletResponse.SC_FORBIDDEN);
            assertThat(investigatorMcpStatus(PING, contentType)).as(contentType).isEqualTo(HttpServletResponse.SC_FORBIDDEN);
        }
        assertThat(investigatorMcpStatus(PING)).as("Content-Type 없음").isEqualTo(HttpServletResponse.SC_FORBIDDEN);
        assertThat(investigatorMcpStatus(PING, "application/json", "application/json; charset=GBK"))
                .as("중복 Content-Type").isEqualTo(HttpServletResponse.SC_FORBIDDEN);
    }

    @Test
    void investigatorMcpRejectsAmbiguousJsonAndAllowsUtf8Reads() throws Exception {
        for (String body : List.of(
                PING + "{" + WRITE_CALL + "}",
                "{" + WRITE_CALL + ",\"method\":\"ping\"}",
                "[" + PING + ",{" + WRITE_CALL + "}]")) {
            assertThat(investigatorMcpStatus(body, "application/json")).as(body).isEqualTo(HttpServletResponse.SC_FORBIDDEN);
        }
        for (String contentType : List.of("application/json", "application/json; charset=utf-8")) {
            assertThat(investigatorMcpStatus(PING, contentType)).as(contentType).isEqualTo(HttpServletResponse.SC_OK);
        }
    }

    @Test
    void investigatorTokenRejectsPaddedValuesAndNeedsTheSharedToken() throws Exception {
        for (String token : List.of(" read-only", "read-only ", "   ")) {
            assertThatThrownBy(() -> new InternalTokenFilter("shared-token", token))
                    .isInstanceOf(IllegalStateException.class);
        }
        InternalTokenFilter withoutShared = new InternalTokenFilter("", "read-only-token");
        MockHttpServletResponse response = new MockHttpServletResponse();
        withoutShared.doFilter(request("GET", "/api/incidents/INC-1", "read-only-token", "investigator"), response, new MockFilterChain());
        assertThat(response.getStatus()).isEqualTo(HttpServletResponse.SC_UNAUTHORIZED);
    }

    private static int investigatorMcpStatus(String body, String... contentTypes) throws Exception {
        InternalTokenFilter filter = new InternalTokenFilter("shared-token", "test-read-only-token");
        MockHttpServletRequest request = request("POST", "/mcp/message", "test-read-only-token", "investigator");
        for (String contentType : contentTypes) {
            request.addHeader("Content-Type", contentType);
        }
        request.setContent(body.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        MockHttpServletResponse response = new MockHttpServletResponse();
        MockFilterChain chain = new MockFilterChain();
        filter.doFilter(request, response, chain);
        assertThat(chain.getRequest() == null).isEqualTo(response.getStatus() != HttpServletResponse.SC_OK);
        return response.getStatus();
    }

    @Test
    void sharedTokenRejectsUnrecognizedRole() throws Exception {
        InternalTokenFilter filter = new InternalTokenFilter("shared-token", "test-read-only-token");
        MockHttpServletRequest request = request("GET", "/api/incidents/INC-1", "shared-token", "unknown-agent");
        MockHttpServletResponse response = new MockHttpServletResponse();
        MockFilterChain chain = new MockFilterChain();
        filter.doFilter(request, response, chain);
        assertThat(response.getStatus()).isEqualTo(HttpServletResponse.SC_FORBIDDEN);
        assertThat(chain.getRequest()).isNull();
    }

    private static MockHttpServletRequest request(String method, String path, String token, String role) {
        MockHttpServletRequest request = new MockHttpServletRequest(method, path);
        request.setServletPath(path);
        request.addHeader("X-Internal-Token", token);
        request.addHeader(InternalRoleContext.ROLE_HEADER, role);
        return request;
    }

    @Test
    void blocksInvestigatorRoleFromCallingCompleteMcpTool() {
        IncidentService incidentService = mock(IncidentService.class);
        CorpusMapper corpusMapper = mock(CorpusMapper.class);
        Validator validator = Validation.buildDefaultValidatorFactory().getValidator();
        IncidentMcpTools tools = new IncidentMcpTools(incidentService, corpusMapper, validator);

        InternalRoleContext.setRole(InternalRoleContext.ROLE_INVESTIGATOR);

        assertThatThrownBy(() -> tools.completeIncidentWithPostMortem(
                "APR-1",
                "INC-2026-001",
                "kubectl rollout undo deployment/payment-service",
                "log-agent",
                "SUCCESS",
                "output",
                "verified",
                "root cause",
                "- timeline",
                "- prevent"
        )).isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("investigator");

        verify(incidentService, never()).completeIncidentWithPostMortem(org.mockito.ArgumentMatchers.any(), org.mockito.ArgumentMatchers.any());
    }

    @Test
    void filterBlocksInvestigatorWritesWith403WhileAllowingReads() throws Exception {
        InternalTokenFilter filter = new InternalTokenFilter("secret-tok");

        MockHttpServletRequest writeReq = new MockHttpServletRequest("POST", "/api/incidents/INC-1/complete");
        writeReq.setServletPath("/api/incidents/INC-1/complete");
        writeReq.addHeader("X-Internal-Token", "secret-tok");
        writeReq.addHeader(InternalRoleContext.ROLE_HEADER, InternalRoleContext.ROLE_INVESTIGATOR);
        MockHttpServletResponse writeResp = new MockHttpServletResponse();
        MockFilterChain writeChain = new MockFilterChain();

        filter.doFilter(writeReq, writeResp, writeChain);

        assertThat(writeResp.getStatus()).isEqualTo(HttpServletResponse.SC_FORBIDDEN);
        assertThat(writeChain.getRequest()).isNull();
        assertThat(InternalRoleContext.getRole()).isNull();

        MockHttpServletRequest readReq = new MockHttpServletRequest("GET", "/api/incidents/INC-1");
        readReq.setServletPath("/api/incidents/INC-1");
        readReq.addHeader("X-Internal-Token", "secret-tok");
        readReq.addHeader(InternalRoleContext.ROLE_HEADER, InternalRoleContext.ROLE_INVESTIGATOR);
        MockHttpServletResponse readResp = new MockHttpServletResponse();
        MockFilterChain readChain = new MockFilterChain();

        filter.doFilter(readReq, readResp, readChain);

        assertThat(readChain.getRequest()).isSameAs(readReq);
        assertThat(InternalRoleContext.getRole()).isNull();

        // MCP POST /mcp/message 서블릿 경계에서 investigator의 쓰기 도구 호출을 Reactor 스레드 전환 전에 403 차단
        MockHttpServletRequest mcpWriteReq = new MockHttpServletRequest("POST", "/mcp/message");
        mcpWriteReq.setServletPath("/mcp/message");
        mcpWriteReq.addHeader("X-Internal-Token", "secret-tok");
        mcpWriteReq.addHeader(InternalRoleContext.ROLE_HEADER, InternalRoleContext.ROLE_INVESTIGATOR);
        mcpWriteReq.setContentType("application/json");
        mcpWriteReq.setContent("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"completeIncidentWithPostMortem\",\"arguments\":{}}}".getBytes(java.nio.charset.StandardCharsets.UTF_8));
        MockHttpServletResponse mcpWriteResp = new MockHttpServletResponse();
        MockFilterChain mcpWriteChain = new MockFilterChain();
        filter.doFilter(mcpWriteReq, mcpWriteResp, mcpWriteChain);
        assertThat(mcpWriteResp.getStatus()).isEqualTo(HttpServletResponse.SC_FORBIDDEN);
        assertThat(mcpWriteChain.getRequest()).isNull();

        // 읽기 전용 MCP 도구(searchCorpusIndex)는 통과하며 캐시된 본문이 다운스트림에 그대로 전달되어야 함
        MockHttpServletRequest mcpReadReq = new MockHttpServletRequest("POST", "/mcp/message");
        mcpReadReq.setServletPath("/mcp/message");
        mcpReadReq.addHeader("X-Internal-Token", "secret-tok");
        mcpReadReq.addHeader(InternalRoleContext.ROLE_HEADER, InternalRoleContext.ROLE_INVESTIGATOR);
        mcpReadReq.setContentType("application/json");
        String readJson = "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"searchCorpusIndex\",\"arguments\":{}}}";
        mcpReadReq.setContent(readJson.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        MockHttpServletResponse mcpReadResp = new MockHttpServletResponse();
        MockFilterChain mcpReadChain = new MockFilterChain();
        filter.doFilter(mcpReadReq, mcpReadResp, mcpReadChain);
        assertThat(mcpReadChain.getRequest()).isNotNull();
        assertThat(new String(mcpReadChain.getRequest().getInputStream().readAllBytes(), java.nio.charset.StandardCharsets.UTF_8))
                .isEqualTo(readJson);
    }
}
