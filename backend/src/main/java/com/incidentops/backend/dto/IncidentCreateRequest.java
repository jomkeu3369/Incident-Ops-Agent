package com.incidentops.backend.dto;

import jakarta.validation.constraints.NotBlank;

public record IncidentCreateRequest(
        @NotBlank(message = "incidentCode는 필수입니다.") String incidentCode,
        @NotBlank(message = "title은 필수입니다.") String title,
        @NotBlank(message = "serviceName은 필수입니다.") String serviceName,
        @NotBlank(message = "severity는 필수입니다.") String severity,
        String summary
) {}