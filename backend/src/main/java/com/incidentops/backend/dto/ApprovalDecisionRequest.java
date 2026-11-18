package com.incidentops.backend.dto;

import jakarta.validation.constraints.NotBlank;

public record ApprovalDecisionRequest(
        @NotBlank(message = "approvedBy는 필수입니다.") String approvedBy,
        @NotBlank(message = "decision은 필수입니다.") String decision
) {}