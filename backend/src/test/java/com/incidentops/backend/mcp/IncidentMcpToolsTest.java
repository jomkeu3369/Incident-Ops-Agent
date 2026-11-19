package com.incidentops.backend.mcp;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.incidentops.backend.domain.CorpusChunk;
import com.incidentops.backend.domain.Incident;
import com.incidentops.backend.mapper.CorpusMapper;
import com.incidentops.backend.service.IncidentService;
import jakarta.validation.Validation;
import java.util.Arrays;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.ai.tool.ToolCallback;

class IncidentMcpToolsTest {
    private final IncidentService incidents = mock(IncidentService.class);
    private final CorpusMapper corpus = mock(CorpusMapper.class);
    private final ObjectMapper json = new ObjectMapper();

    @Test
    void registersFourToolsAndAdvertisesTheCorpusTypes() throws Exception {
        try (var validation = Validation.buildDefaultValidatorFactory()) {
            var tools = new IncidentMcpTools(incidents, corpus, validation.getValidator());
            var callbacks = new IncidentMcpConfig().incidentToolCallbacks(tools).getToolCallbacks();

            assertThat(callbacks).extracting(callback -> callback.getToolDefinition().name())
                    .containsExactlyInAnyOrder("searchCorpusIndex", "pullCorpusChunk",
                            "getIncidentDetail", "completeIncidentWithPostMortem");

            JsonNode schema = json.readTree(callbackNamed(callbacks, "searchCorpusIndex").getToolDefinition().inputSchema());
            assertThat(schema.at("/properties/corpusType/enum")).extracting(JsonNode::asText)
                    .containsExactlyInAnyOrder("LOG", "RUNBOOK");
            assertThat(schema.get("required")).extracting(JsonNode::asText)
                    .containsExactlyInAnyOrder("incidentCode", "corpusType");
        }
    }

    @Test
    void searchReturnsMetadataFromTheIncidentScopeWithoutContent() throws Exception {
        try (var validation = Validation.buildDefaultValidatorFactory()) {
            var tools = new IncidentMcpTools(incidents, corpus, validation.getValidator());
            var callbacks = new IncidentMcpConfig().incidentToolCallbacks(tools).getToolCallbacks();
            when(incidents.getIncident("INC-1")).thenReturn(
                    Incident.builder().incidentCode("INC-1").serviceName("payments").build());
            when(corpus.searchMetadata("INC-1", "payments", "LOG", "timeout")).thenReturn(
                    List.of(CorpusChunk.builder().chunkId("LOG-1").contentLength(120).build()));

            String result = callbackNamed(callbacks, "searchCorpusIndex").call("""
                    {"incidentCode":"INC-1","corpusType":"LOG","keyword":"timeout"}
                    """);

            JsonNode hit = json.readTree(result).get(0);
            assertThat(hit.get("chunkId").asText()).isEqualTo("LOG-1");
            assertThat(hit.get("contentLength").asInt()).isEqualTo(120);
            assertThat(hit.has("content")).isFalse();
        }
    }

    @Test
    void completionValidatesTheRestRequestBeforeCallingTheService() {
        try (var validation = Validation.buildDefaultValidatorFactory()) {
            var tools = new IncidentMcpTools(incidents, corpus, validation.getValidator());

            assertThatThrownBy(() -> tools.completeIncidentWithPostMortem(
                    null, "INC-1", "recorded demo action", "operator", "FAILED", null, null, null, null, null))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessageContaining("approvalId");
            verifyNoInteractions(incidents);
        }
    }

    private ToolCallback callbackNamed(ToolCallback[] callbacks, String name) {
        return Arrays.stream(callbacks).filter(callback -> name.equals(callback.getToolDefinition().name()))
                .findFirst()
                .orElseThrow();
    }
}
