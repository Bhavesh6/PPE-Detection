// Common utility functions shared across pages.

// Fade in elements with animation when page loads
document.addEventListener('DOMContentLoaded', function() {
    const fadeElements = document.querySelectorAll('.fade-in');
    fadeElements.forEach(element => {
        element.classList.add('visible');
    });
});

// Navigation highlighting
function highlightCurrentPage() {
    const currentPath = window.location.pathname;
    const navLinks = document.querySelectorAll('nav a');

    navLinks.forEach(link => {
        const linkPath = link.getAttribute('href');
        if (linkPath && currentPath.endsWith(linkPath)) {
            link.classList.add('active-link');
        }
    });
}

document.addEventListener('DOMContentLoaded', function() {
    highlightCurrentPage();
});
