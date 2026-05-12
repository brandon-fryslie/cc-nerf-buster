package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// CertAuthority holds the CA key pair and caches generated leaf certs.
type CertAuthority struct {
	caCert    *x509.Certificate
	caKey     *ecdsa.PrivateKey
	caTLS     tls.Certificate // PEM-decoded, for tls.Config
	certCache sync.Map        // host -> *tls.Certificate
}

// LoadOrCreateCA loads an existing CA from dataDir, or generates a new one.
func LoadOrCreateCA(dataDir string) (*CertAuthority, error) {
	certPath := filepath.Join(dataDir, "ca.crt")
	keyPath := filepath.Join(dataDir, "ca.key")

	certPEM, certErr := os.ReadFile(certPath)
	keyPEM, keyErr := os.ReadFile(keyPath)

	if certErr == nil && keyErr == nil {
		return loadCA(certPEM, keyPEM)
	}

	// Generate new CA
	ca, certPEM, keyPEM, err := generateCA()
	if err != nil {
		return nil, fmt.Errorf("generate CA: %w", err)
	}

	if err := os.WriteFile(certPath, certPEM, 0644); err != nil {
		return nil, fmt.Errorf("write CA cert: %w", err)
	}
	if err := os.WriteFile(keyPath, keyPEM, 0600); err != nil {
		return nil, fmt.Errorf("write CA key: %w", err)
	}

	fmt.Fprintf(os.Stderr, "Generated new CA certificate: %s\n", certPath)
	fmt.Fprintf(os.Stderr, "Trust it on macOS with:\n")
	fmt.Fprintf(os.Stderr, "  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain %s\n", certPath)

	return ca, nil
}

func loadCA(certPEM, keyPEM []byte) (*CertAuthority, error) {
	tlsCert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, fmt.Errorf("parse CA keypair: %w", err)
	}

	caCert, err := x509.ParseCertificate(tlsCert.Certificate[0])
	if err != nil {
		return nil, fmt.Errorf("parse CA certificate: %w", err)
	}

	caKey, ok := tlsCert.PrivateKey.(*ecdsa.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("CA key is not ECDSA")
	}

	return &CertAuthority{
		caCert: caCert,
		caKey:  caKey,
		caTLS:  tlsCert,
	}, nil
}

func generateCA() (*CertAuthority, []byte, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, err
	}

	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return nil, nil, nil, err
	}

	template := &x509.Certificate{
		SerialNumber: serial,
		Subject: pkix.Name{
			CommonName:   "cc-nerf-buster CA",
			Organization: []string{"cc-nerf-buster"},
		},
		NotBefore:             time.Now().Add(-1 * time.Hour),
		NotAfter:              time.Now().Add(10 * 365 * 24 * time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
		MaxPathLen:            0,
	}

	certDER, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return nil, nil, nil, err
	}

	cert, err := x509.ParseCertificate(certDER)
	if err != nil {
		return nil, nil, nil, err
	}

	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certDER})
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return nil, nil, nil, err
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})

	tlsCert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, nil, nil, err
	}

	return &CertAuthority{
		caCert: cert,
		caKey:  key,
		caTLS:  tlsCert,
	}, certPEM, keyPEM, nil
}

// CertForHost returns a TLS certificate for the given hostname,
// signed by this CA. Results are cached; expired certs are regenerated.
func (ca *CertAuthority) CertForHost(host string) (*tls.Certificate, error) {
	if cached, ok := ca.certCache.Load(host); ok {
		cert := cached.(*tls.Certificate)
		leaf, err := x509.ParseCertificate(cert.Certificate[0])
		if err == nil && time.Now().Before(leaf.NotAfter.Add(-5*time.Minute)) {
			return cert, nil
		}
		ca.certCache.Delete(host)
	}

	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}

	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return nil, err
	}

	template := &x509.Certificate{
		SerialNumber: serial,
		Subject: pkix.Name{
			CommonName: host,
		},
		DNSNames:  []string{host},
		NotBefore: time.Now().Add(-1 * time.Hour),
		NotAfter:  time.Now().Add(10 * 365 * 24 * time.Hour),
		KeyUsage:  x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{
			x509.ExtKeyUsageServerAuth,
		},
	}

	certDER, err := x509.CreateCertificate(rand.Reader, template, ca.caCert, &key.PublicKey, ca.caKey)
	if err != nil {
		return nil, err
	}

	tlsCert := &tls.Certificate{
		Certificate: [][]byte{certDER, ca.caTLS.Certificate[0]},
		PrivateKey:  key,
	}

	ca.certCache.Store(host, tlsCert)
	return tlsCert, nil
}
