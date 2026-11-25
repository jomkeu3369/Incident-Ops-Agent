package com.incidentops.backend.mcp;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.incidentops.backend.domain.Incident;
import com.incidentops.backend.service.IncidentService;
import io.modelcontextprotocol.server.McpServerFeatures;
import io.modelcontextprotocol.spec.McpSchema;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.ai.tool.ToolCallbackProvider;
import org.springframework.ai.tool.method.MethodToolCallbackProvider;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration(proxyBeanMethods = false)
class IncidentMcpConfig {

    static final String EVIDENCE_SUMMARY_URI = "incident://evidence-summary/v1";

    @Bean
    ToolCallbackProvider incidentToolCallbacks(IncidentMcpTools tools) {
        return MethodToolCallbackProvider.builder().toolObjects(tools).build();
    }

    @Bean
    List<McpServerFeatures.SyncResourceSpecification> incidentMcpResources(
            IncidentService incidentService,
            ObjectMapper objectMapper
    ) {
        McpSchema.Resource summaryResource = new McpSchema.Resource(
                EVIDENCE_SUMMARY_URI,
                "incident-evidence-summary-v1",
                "Versioned read-only summary of registered incidents and their current operational status.",
                "application/json",
                null
        );

        McpServerFeatures.SyncResourceSpecification spec = new McpServerFeatures.SyncResourceSpecification(
                summaryResource,
                (exchange, request) -> {
                    List<Incident> incidents = incidentService.getAllIncidents();
                    List<Map<String, Object>> items = incidents.stream().map(inc -> {
                        Map<String, Object> row = new LinkedHashMap<>();
                        row.put("incidentCode", inc.getIncidentCode());
                        row.put("title", inc.getTitle());
                        row.put("serviceName", inc.getServiceName());
                        row.put("severity", inc.getSeverity());
                        row.put("status", inc.getStatus());
                        row.put("summary", inc.getSummary());
                        return row;
                    }).toList();

                    Map<String, Object> payload = new LinkedHashMap<>();
                    payload.put("schemaVersion", "v1");
                    payload.put("readOnly", true);
                    payload.put("resourceUri", EVIDENCE_SUMMARY_URI);
                    payload.put("incidentCount", items.size());
                    payload.put("incidents", items);

                    try {
                        String json = objectMapper.writeValueAsString(payload);
                        return new McpSchema.ReadResourceResult(
                                List.of(new McpSchema.TextResourceContents(EVIDENCE_SUMMARY_URI, "application/json", json))
                        );
                    } catch (JsonProcessingException ex) {
                        throw new IllegalStateException("근거 요약 리소스를 JSON으로 직렬화할 수 없습니다.", ex);
                    }
                }
        );

        return List.of(spec);
    }
}
