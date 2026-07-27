package Foundation {
    doc /* Cross-cutting: config, DB schema, logging. Shared dependency into
         * all subsystems, not a peer subsystem. Corresponds to
         * config-db-paths-and-domains, db-schema-ddl-and-migrations, and
         * logger-sqlite-handler in concept_store/concepts.json. */
    part def Foundation;
}
