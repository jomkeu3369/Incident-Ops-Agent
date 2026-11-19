package com.incidentops.backend.config;

import io.swagger.v3.oas.annotations.OpenAPIDefinition;
import io.swagger.v3.oas.annotations.enums.SecuritySchemeIn;
import io.swagger.v3.oas.annotations.enums.SecuritySchemeType;
import io.swagger.v3.oas.annotations.security.SecurityRequirement;
import io.swagger.v3.oas.annotations.security.SecurityScheme;
import org.springframework.context.annotation.Configuration;

@Configuration(proxyBeanMethods = false)
@OpenAPIDefinition(security = @SecurityRequirement(name = "internalToken"))
@SecurityScheme(name = "internalToken", type = SecuritySchemeType.APIKEY,
        in = SecuritySchemeIn.HEADER, paramName = InternalTokenFilter.HEADER_NAME)
public class OpenApiConfig {
}
