package com.incidentops.backend.dto;

import com.incidentops.backend.domain.RemediationPlanStep;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import java.util.List;

public record PlanCreateRequest(
        @NotBlank @Size(max = 64) String planId,
        @NotBlank @Size(max = 100) String createdBy,
        @NotEmpty List<@NotNull @Valid RemediationPlanStep> steps,
        @NotBlank String verificationCriteria
) {}
