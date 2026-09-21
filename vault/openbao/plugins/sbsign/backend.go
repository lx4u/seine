package main

import (
	"context"

	"github.com/openbao/openbao/sdk/v2/framework"
	"github.com/openbao/openbao/sdk/v2/logical"
)

// Matches the routes vault-plugins/sbsign/poc.py expects. Sign paths
// never mint: unknown keys answer 404, only generate/import creates
// them; /cert exists only for sbverify checks.
func Factory(ctx context.Context, conf *logical.BackendConfig) (logical.Backend, error) {
	return Backend(), nil
}

func Backend() *framework.Backend {
	return &framework.Backend{
		Paths: []*framework.Path{
			pathKeys(),
			pathCert(),
			pathSign(),
			pathSignVar(),
		},
		Secrets:     []*framework.Secret{},
		BackendType: logical.TypeLogical,
	}
}

func pathKeys() *framework.Path {
	return &framework.Path{
		Pattern: "keys/(?P<name>[^/]+)",
		Fields: map[string]*framework.FieldSchema{
			"name": {
				Type:        framework.TypeString,
				Description: "Key name",
			},
			"generate": {
				Type:        framework.TypeMap,
				Description: "Mint a new keypair in the vault",
			},
			"import": {
				Type:        framework.TypeMap,
				Description: "Store an existing keypair in the vault",
			},
			"overwrite": {
				Type:        framework.TypeBool,
				Description: "Replace a key that already exists",
			},
		},
		Operations: map[logical.Operation]framework.OperationHandler{
			logical.CreateOperation: &framework.PathOperation{
				Callback: handleKeyMgmt,
			},
			logical.UpdateOperation: &framework.PathOperation{
				Callback: handleKeyMgmt,
			},
		},
	}
}

func pathCert() *framework.Path {
	return &framework.Path{
		Pattern: "keys/(?P<name>[^/]+)/cert",
		Fields: map[string]*framework.FieldSchema{
			"name": {
				Type:        framework.TypeString,
				Description: "Key name",
			},
		},
		Operations: map[logical.Operation]framework.OperationHandler{
			logical.ReadOperation: &framework.PathOperation{
				Callback: handleCert,
			},
		},
	}
}

func pathSign() *framework.Path {
	return &framework.Path{
		Pattern: "keys/(?P<name>[^/]+)/sign",
		Fields: map[string]*framework.FieldSchema{
			"name": {
				Type:        framework.TypeString,
				Description: "Key name",
			},
			"pe_base64": {
				Type:        framework.TypeString,
				Description: "Base64 of the PE binary to sign",
			},
			"signing_time": {
				Type:        framework.TypeString,
				Description: "Signature creation time, RFC3339",
			},
		},
		Operations: map[logical.Operation]framework.OperationHandler{
			logical.CreateOperation: &framework.PathOperation{
				Callback: handleSign,
			},
			logical.UpdateOperation: &framework.PathOperation{
				Callback: handleSign,
			},
		},
	}
}

func pathSignVar() *framework.Path {
	return &framework.Path{
		Pattern: "keys/(?P<name>[^/]+)/sign-var",
		Fields: map[string]*framework.FieldSchema{
			"name": {
				Type:        framework.TypeString,
				Description: "Key name",
			},
			"var": {
				Type:        framework.TypeString,
				Description: "UEFI variable name (PK, KEK, db, dbx, ...)",
			},
			"guid": {
				Type:        framework.TypeString,
				Description: "Vendor GUID, dashed hex",
			},
			"esl_base64": {
				Type:        framework.TypeString,
				Description: "Base64 of the new EFI Signature List value",
			},
			"attributes": {
				Type:        framework.TypeInt,
				Description: "Variable attributes; defaults to 0x27",
			},
			"signing_time": {
				Type:        framework.TypeString,
				Description: "Signature creation time, RFC3339",
			},
		},
		Operations: map[logical.Operation]framework.OperationHandler{
			logical.CreateOperation: &framework.PathOperation{
				Callback: handleSignVar,
			},
			logical.UpdateOperation: &framework.PathOperation{
				Callback: handleSignVar,
			},
		},
	}
}
