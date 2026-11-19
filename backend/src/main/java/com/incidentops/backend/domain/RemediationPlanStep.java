package com.incidentops.backend.domain;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;
import java.util.List;

public record RemediationPlanStep(
        @NotNull @Positive Integer stepNo,
        @NotBlank String description,
        String condition,
        String actionCommand,
        @NotNull List<@NotBlank @Size(max = 32) String> evidenceChunkIds
) {}
