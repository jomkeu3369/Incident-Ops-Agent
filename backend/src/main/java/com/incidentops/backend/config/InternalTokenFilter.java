package com.incidentops.backend.config;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

@Component
public class InternalTokenFilter extends OncePerRequestFilter {

    private static final String HEADER_NAME = "X-Internal-Token";
    private static final String LOCAL_DEV_DEFAULT = "local-dev-token";

    @Value("${app.internal-service-token:local-dev-token}")
    private String expectedToken;

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        String path = request.getRequestURI();
        return path.startsWith("/actuator/health")
                || path.startsWith("/swagger-ui")
                || path.startsWith("/v3/api-docs");
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain) throws ServletException, IOException {

        String providedToken = request.getHeader(HEADER_NAME);

        if (LOCAL_DEV_DEFAULT.equals(expectedToken) && (providedToken == null || providedToken.isBlank())) {
            filterChain.doFilter(request, response);
            return;
        }

        if (providedToken == null || !expectedToken.equals(providedToken)) {
            response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
            response.setContentType(MediaType.APPLICATION_JSON_VALUE);
            response.setCharacterEncoding("UTF-8");
            response.getWriter().write("{\"status\":401,\"error\":\"Unauthorized\",\"message\":\"유효하지 않은 내부 서비스 토큰입니다.\"}");
            return;
        }

        filterChain.doFilter(request, response);
    }
}