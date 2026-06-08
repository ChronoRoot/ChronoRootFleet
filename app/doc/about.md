# Welcome to the ChronoRoot Fleet Controller

A centralized web operations hub to orchestrate, monitor, and maintain fleets of ChronoRoot modules for high-throughput temporal plant phenotyping.

## System Description

While individual ChronoRoot edge modules handle the physical capturing of in-vitro plant roots using multiplexed cameras and IR backlights, the **ChronoRoot Fleet Controller** acts as the central orchestrator for the entire laboratory.

Instead of managing growth chamber cameras one by one, the Fleet Controller allows researchers to monitor global hardware health, push configurations, and launch synchronized biological imaging batches across dozens of isolated Raspberry Pi nodes simultaneously.

Built on a robust, asynchronous "Passive Aggregation" architecture, the Master Controller ensures strict network isolation. It bridges the gap between hardware arrays and the end-user via a Zero-Touch Reverse Proxy, meaning the edge modules operate completely autonomously while the controller observes, orchestrates, and securely logs their scientific progress into a permanent relational database.

---

## About ChronoRoot Fleet Controller

**Copyright:** 2026- IPS2

**Version:** v1.0.0

**Licence:** CeCILL v2.1 OR GNU GPL v3

### Contributors

* Nicolás Gaggion

### Links

* [IPS2 Website](http://ips2.u-psud.fr)
* [ChronoRoot Main Website](https://chronoroot.github.io/)
* [ChronoRoot Fleet Controller Source Code](https://github.com/ChronoRoot/FleetControl)
* [ChronoRoot Module Controller Source Code](https://github.com/ChronoRoot/ChronoRootControl)
* [ChronoRoot Image Analysis Pipeline](https://github.com/ChronoRoot/ChronoRoot2)

---

## References

If you use the ChronoRoot ecosystem in your research, please cite the following publications:

### ChronoRoot 2.0 (2026)

**ChronoRoot 2.0: an open AI-powered platform for 2D temporal plant phenotyping**

*Gaggion, N., Boccardo, N.A., Bonazzola, R., et al.*

GigaScience, Volume 15, January 2026.

[doi: 10.1093/gigascience/giag018](https://doi.org/10.1093/gigascience/giag018)

```bibtex
@article{10.1093/gigascience/giag018,
    author = {Gaggion, Nicolás and Boccardo, Noelia A and Bonazzola, Rodrigo and Legascue, María Florencia and Mammarella, María Florencia and Rodriguez, Florencia Sol and Aballay, Federico Emanuel and Catulo, Florencia Belén and Barrios, Andana and Santoro, Luciano J and Accavallo, Franco and Villarreal, Santiago Nahuel and Pereyra-Bistrain, Leonardo I and Benhamed, Moussa and Crespi, Martin and Ricardi, Martiniano María and Petrillo, Ezequiel and Blein, Thomas and Ariel, Federico and Ferrante, Enzo},
    title = {ChronoRoot 2.0: an open AI-powered platform for 2D temporal plant phenotyping},
    journal = {GigaScience},
    volume = {15},
    pages = {giag018},
    year = {2026},
    month = {01},
    issn = {2047-217X},
    doi = {10.1093/gigascience/giag018},
}

```

### ChronoRoot 1.0 (2021)

***ChronoRoot: High-throughput phenotyping by deep segmentation networks reveals novel temporal parameters of plant root system architecture***

*Gaggion, N., Ariel, F., Daric, V., et al.*

GigaScience, Volume 10, July 2021.
[doi: 10.1093/gigascience/giab052](https://doi.org/10.1093/gigascience/giab052)

```bibtex
@article{10.1093/gigascience/giab052,
    author = {Gaggion, Nicolás and Ariel, Federico and Daric, Vladimir and Lambert, Éric and Legendre, Simon and Roulé, Thomas and Camoirano, Alejandra and Milone, Diego H and Crespi, Martin and Blein, Thomas and Ferrante, Enzo},
    title = {ChronoRoot: High-throughput phenotyping by deep segmentation networks reveals novel temporal parameters of plant root system architecture},
    journal = {GigaScience},
    volume = {10},
    number = {7},
    pages = {giab052},
    year = {2021},
    month = {07},
    issn = {2047-217X},
    doi = {10.1093/gigascience/giab052},
}

```