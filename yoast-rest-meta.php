<?php
/**
 * Plugin Name: Yoast REST meta (write access)
 * Description: Exposes Yoast SEO title/description/focus keyword as writable
 *              post meta on /wp/v2/posts so automation can set them.
 * Install: drop into wp-content/mu-plugins/
 */
add_action('init', function () {
    $keys = ['_yoast_wpseo_title', '_yoast_wpseo_metadesc', '_yoast_wpseo_focuskw'];
    foreach ($keys as $key) {
        register_post_meta('post', $key, [
            'type'          => 'string',
            'single'        => true,
            'show_in_rest'  => true,
            'sanitize_callback' => 'sanitize_text_field',
            // Protected (underscore) meta: only users who can edit the post may write it.
            'auth_callback' => function ($allowed, $meta_key, $post_id) {
                return current_user_can('edit_post', $post_id);
            },
        ]);
    }
});
