package com.incidentops.backend.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

public record ApprovalCreateRequest(
        @NotBlank @Size(max = 64) String approvalId,
        @NotBlank @Size(max = 64) String planId,
        @NotNull @Positive Integer stepNo,
        @NotBlank @Size(max = 100) String proposedBy
) {}
