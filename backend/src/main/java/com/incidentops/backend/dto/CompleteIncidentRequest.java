package com.incidentops.backend.dto;

import jakarta.validation.constraints.NotBlank;

public record CompleteIncidentRequest(
        @NotBlank(message = "approvalId는 필수입니다.") String approvalId,
        @NotBlank(message = "actionCommand는 필수입니다.") String actionCommand,
        @NotBlank(message = "executedBy는 필수입니다.") String executedBy,
        @NotBlank(message = "executionStatus는 필수입니다.") String executionStatus,
        String executionOutput,
        String rootCause,
        String timelineMarkdown,
        String remediationSummary,
        String preventionItems
) {}