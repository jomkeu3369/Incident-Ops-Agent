package com.incidentops.backend.config;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

@Component
public class InternalTokenFilter extends OncePerRequestFilter {

    static final String HEADER_NAME = "X-Internal-Token";

    private final String expectedToken;

    public InternalTokenFilter(@Value("${app.internal-service-token:}") String expectedToken) {
        this.expectedToken = expectedToken == null ? "" : expectedToken;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        String path = request.getServletPath();
        return path.equals("/actuator/health") || path.startsWith("/actuator/health/")
                || path.equals("/swagger-ui.html") || path.startsWith("/swagger-ui/")
                || path.equals("/v3/api-docs") || path.startsWith("/v3/api-docs/")
                || path.equals("/v3/api-docs.yaml");
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain) throws ServletException, IOException {

        String providedToken = request.getHeader(HEADER_NAME);

        if (!constantTimeEquals(expectedToken, providedToken)) {
            response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
            response.setContentType(MediaType.APPLICATION_JSON_VALUE);
            response.setCharacterEncoding("UTF-8");
            response.setHeader("X-Content-Type-Options", "nosniff");
            response.setHeader("Cache-Control", "no-store");
            response.getWriter().write("{\"status\":401,\"error\":\"Unauthorized\",\"message\":\"유효하지 않은 내부 서비스 토큰입니다.\"}");
            return;
        }

        filterChain.doFilter(request, response);
    }

    static boolean constantTimeEquals(String expected, String provided) {
        if (expected == null || expected.isBlank() || provided == null) {
            return false;
        }
        return MessageDigest.isEqual(
                expected.getBytes(StandardCharsets.UTF_8),
                provided.getBytes(StandardCharsets.UTF_8));
    }
}
