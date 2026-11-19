package com.incidentops.backend.mcp;

import com.incidentops.backend.domain.CorpusChunk;
import com.incidentops.backend.domain.Incident;
import com.incidentops.backend.dto.CompleteIncidentRequest;
import com.incidentops.backend.dto.CompleteIncidentResponse;
import com.incidentops.backend.dto.IncidentDetailResponse;
import com.incidentops.backend.exception.ResourceNotFoundException;
import com.incidentops.backend.mapper.CorpusMapper;
import com.incidentops.backend.service.IncidentService;
import jakarta.validation.ConstraintViolation;
import jakarta.validation.Validator;
import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;
import lombok.RequiredArgsConstructor;
import org.springframework.ai.tool.annotation.Tool;
import org.springframework.ai.tool.annotation.ToolParam;
import org.springframework.stereotype.Component;
import org.springframework.util.Assert;

@Component
@RequiredArgsConstructor
public class IncidentMcpTools {
    private final IncidentService incidentService;
    private final CorpusMapper corpusMapper;
    private final Validator validator;

    public enum CorpusType {
        LOG, RUNBOOK
    }

    @Tool(description = "Search metadata for logs attached to an incident or runbooks for its service.")
    public List<CorpusChunk> searchCorpusIndex(
            @ToolParam(description = "Incident code") String incidentCode,
            @ToolParam(description = "Corpus type: LOG or RUNBOOK") CorpusType corpusType,
            @ToolParam(description = "Optional substring in the title, keywords or service name", required = false)
            String keyword
    ) {
        Assert.hasText(incidentCode, "incidentCode는 필수입니다.");
        Assert.notNull(corpusType, "corpusType은 LOG 또는 RUNBOOK이어야 합니다.");
        Incident incident = incidentService.getIncident(incidentCode);
        return corpusMapper.searchMetadata(
                incident.getIncidentCode(), incident.getServiceName(), corpusType.name(), keyword);
    }

    @Tool(description = "Fetch the full text of a log or runbook chunk in the incident's corpus.")
    public CorpusChunk pullCorpusChunk(
            @ToolParam(description = "Incident code") String incidentCode,
            @ToolParam(description = "Chunk ID from the corpus index") String chunkId
    ) {
        Assert.hasText(incidentCode, "incidentCode는 필수입니다.");
        Assert.hasText(chunkId, "chunkId는 필수입니다.");
        Incident incident = incidentService.getIncident(incidentCode);
        CorpusChunk chunk = corpusMapper.findByChunkId(incident.getIncidentCode(), incident.getServiceName(), chunkId);
        if (chunk == null) {
            throw new ResourceNotFoundException("해당 장애에서 조회할 수 있는 청크를 찾지 못했습니다.");
        }
        return chunk;
    }

    @Tool(description = "Read an incident with its recovery plans, approvals, execution history and postmortem.")
    public IncidentDetailResponse getIncidentDetail(@ToolParam(description = "Incident code") String incidentCode) {
        Assert.hasText(incidentCode, "incidentCode는 필수입니다.");
        return incidentService.getIncidentDetail(incidentCode);
    }

    @Tool(description = "Record the result of an approved recovery action. On SUCCESS, resolve the incident and create its postmortem.")
    public CompleteIncidentResponse completeIncidentWithPostMortem(
            @ToolParam(description = "Approved recovery action ID") String approvalId,
            @ToolParam(description = "Incident code") String incidentCode,
            @ToolParam(description = "Command recorded in the approval") String actionCommand,
            @ToolParam(description = "Executor of the approved action") String executedBy,
            @ToolParam(description = "Execution outcome: SUCCESS or FAILED") String executionStatus,
            @ToolParam(description = "Execution output", required = false) String executionOutput,
            @ToolParam(description = "Evidence that the recovery criteria were met, required on SUCCESS", required = false)
            String verificationSummary,
            @ToolParam(description = "Root cause, required on SUCCESS", required = false) String rootCause,
            @ToolParam(description = "Incident timeline in Markdown, required on SUCCESS", required = false)
            String timelineMarkdown,
            @ToolParam(description = "Preventive actions, required on SUCCESS", required = false) String preventionItems
    ) {
        Assert.hasText(incidentCode, "incidentCode는 필수입니다.");
        CompleteIncidentRequest request = new CompleteIncidentRequest(
                approvalId, actionCommand, executedBy, executionStatus, executionOutput,
                verificationSummary, rootCause, timelineMarkdown, null, preventionItems);
        Set<ConstraintViolation<CompleteIncidentRequest>> violations = validator.validate(request);
        if (!violations.isEmpty()) {
            String message = violations.stream()
                    .map(ConstraintViolation::getMessage)
                    .sorted()
                    .collect(Collectors.joining(" "));
            throw new IllegalArgumentException(message);
        }
        return incidentService.completeIncidentWithPostMortem(incidentCode, request);
    }
}
