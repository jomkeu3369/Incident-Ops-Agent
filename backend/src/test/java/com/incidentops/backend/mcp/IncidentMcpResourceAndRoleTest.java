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
        inc.setTitle("결제 API 502 급증");
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
        assertThat(root.get("incidents").get(0).get("incidentCode").asText()).isEqualTo("INC-2026-001");
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
    }
}
