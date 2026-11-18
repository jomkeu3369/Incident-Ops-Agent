package com.incidentops.backend.dto;

import jakarta.validation.constraints.NotBlank;

public record ApprovalCreateRequest(
        @NotBlank(message = "approvalId는 필수입니다.") String approvalId,
        @NotBlank(message = "actionCommand는 필수입니다.") String actionCommand,
        @NotBlank(message = "proposedBy는 필수입니다.") String proposedBy
) {}